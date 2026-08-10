"""Playwright trace, screenshot, video, console, and network evidence collector."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from src.models.schemas import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceType,
    InvestigationContext,
    TestFailureInfo,
)
from src.plugins.base import BaseCollector
from src.utils.logging import get_logger

logger = get_logger(__name__)


class PlaywrightArtifactsCollector(BaseCollector):
    """
    Collects and lightly parses Playwright artifacts.
    Expects local paths or downloadable URIs in context.extra or failed_tests.
    Production deployments should point artifact storage (ADO, Blob, S3).
    """

    name = "playwright_artifacts"
    priority = 30

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self.artifact_root = Path((config or {}).get("artifact_root", "./data/artifacts"))

    async def collect(self, context: InvestigationContext) -> EvidenceBundle:
        items: list[EvidenceItem] = []

        for test in context.failed_tests:
            # Screenshots
            for uri in test.screenshot_uris:
                items.append(
                    EvidenceItem(
                        type=EvidenceType.SCREENSHOT,
                        source=self.name,
                        title=f"Screenshot for {test.test_title}",
                        content={"uri": uri, "test": test.test_title},
                        raw_uri=uri,
                        related_entities=[test.test_title],
                    )
                )

            if test.video_uri:
                items.append(
                    EvidenceItem(
                        type=EvidenceType.VIDEO,
                        source=self.name,
                        title=f"Video for {test.test_title}",
                        content={"uri": test.video_uri},
                        raw_uri=test.video_uri,
                        related_entities=[test.test_title],
                    )
                )

            if test.trace_uri:
                parsed = await self._parse_trace(test.trace_uri, test)
                items.extend(parsed)

            for err in test.console_errors:
                items.append(
                    EvidenceItem(
                        type=EvidenceType.CONSOLE_LOG,
                        source=self.name,
                        title=f"Console error: {test.test_title}",
                        content=err,
                        related_entities=[test.test_title],
                    )
                )

            for net in test.network_failures:
                items.append(
                    EvidenceItem(
                        type=EvidenceType.NETWORK_REQUEST,
                        source=self.name,
                        title=f"Network failure: {net.get('url', 'unknown')}",
                        content=net,
                        related_entities=[test.test_title, net.get("url", "")],
                    )
                )

        # Also scan local artifact directory for any leftover traces
        if self.artifact_root.exists():
            for trace_path in self.artifact_root.rglob("*.zip"):
                if "trace" in trace_path.name.lower():
                    items.append(
                        EvidenceItem(
                            type=EvidenceType.PLAYWRIGHT_TRACE,
                            source=self.name,
                            title=f"Local trace: {trace_path.name}",
                            content={"path": str(trace_path)},
                            raw_uri=str(trace_path),
                        )
                    )

        return EvidenceBundle(collector=self.name, items=items)

    async def _parse_trace(self, uri: str, test: TestFailureInfo) -> list[EvidenceItem]:
        """Best-effort parse of a Playwright trace zip (local path preferred)."""
        items: list[EvidenceItem] = []
        path = Path(uri)
        if not path.exists():
            items.append(
                EvidenceItem(
                    type=EvidenceType.PLAYWRIGHT_TRACE,
                    source=self.name,
                    title=f"Trace reference: {test.test_title}",
                    content={"uri": uri, "note": "remote or missing locally"},
                    raw_uri=uri,
                    related_entities=[test.test_title],
                )
            )
            return items

        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = zf.namelist()
                # Look for common Playwright trace files
                for name in names:
                    if name.endswith(".json") and "trace" in name.lower():
                        try:
                            raw = zf.read(name).decode("utf-8", errors="replace")
                            data = json.loads(raw)
                            # Extract console & network events if present
                            events = data if isinstance(data, list) else data.get("events", [])
                            console_events = [
                                e
                                for e in events
                                if isinstance(e, dict)
                                and e.get("type") in ("console", "pageerror")
                            ][:30]
                            network_events = [
                                e
                                for e in events
                                if isinstance(e, dict) and e.get("type") in ("request", "response", "requestfailed")
                            ][:50]
                            if console_events:
                                items.append(
                                    EvidenceItem(
                                        type=EvidenceType.CONSOLE_LOG,
                                        source=self.name,
                                        title=f"Trace console events: {test.test_title}",
                                        content=console_events,
                                        related_entities=[test.test_title],
                                    )
                                )
                            if network_events:
                                items.append(
                                    EvidenceItem(
                                        type=EvidenceType.NETWORK_REQUEST,
                                        source=self.name,
                                        title=f"Trace network events: {test.test_title}",
                                        content=network_events,
                                        related_entities=[test.test_title],
                                    )
                                )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("trace_json_parse_skip", file=name, error=str(exc))

                items.append(
                    EvidenceItem(
                        type=EvidenceType.PLAYWRIGHT_TRACE,
                        source=self.name,
                        title=f"Parsed trace: {path.name}",
                        content={"files": names[:100], "path": str(path)},
                        raw_uri=str(path),
                        related_entities=[test.test_title],
                    )
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("trace_parse_failed", path=str(path), error=str(exc))
            items.append(
                EvidenceItem(
                    type=EvidenceType.PLAYWRIGHT_TRACE,
                    source=self.name,
                    title=f"Trace (unparsed): {path.name}",
                    content={"error": str(exc), "path": str(path)},
                    raw_uri=str(path),
                )
            )
        return items
