"""Azure DevOps pipeline, run, and artifact collector."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config.settings import get_settings
from src.models.schemas import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceType,
    InvestigationContext,
    PipelineContext,
    TestFailureInfo,
)
from src.plugins.base import BaseCollector
from src.utils.logging import get_logger

logger = get_logger(__name__)


class AzureDevOpsCollector(BaseCollector):
    name = "azure_devops"
    priority = 10

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        settings = get_settings()
        self.org_url = (config or {}).get("org_url") or settings.azure_devops_org_url
        self.pat = (config or {}).get("pat") or (
            settings.azure_devops_pat.get_secret_value() if settings.azure_devops_pat else None
        )
        self.project = (config or {}).get("project") or settings.azure_devops_project

    def _headers(self) -> dict[str, str]:
        import base64

        if not self.pat:
            return {}
        token = base64.b64encode(f":{self.pat}".encode()).decode()
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    async def _get(self, client: httpx.AsyncClient, url: str) -> dict[str, Any]:
        resp = await client.get(url, headers=self._headers(), timeout=30.0)
        resp.raise_for_status()
        return resp.json()

    async def collect(self, context: InvestigationContext) -> EvidenceBundle:
        items: list[EvidenceItem] = []
        pipeline = context.pipeline

        if not self.pat:
            logger.warning("azure_devops_pat_missing")
            return EvidenceBundle(
                collector=self.name,
                items=[],
                errors=["AZURE_DEVOPS_PAT not configured"],
            )

        async with httpx.AsyncClient() as client:
            # Build / run details
            run_url = (
                f"{pipeline.org_url}/{pipeline.project}/_apis/pipelines/"
                f"{pipeline.pipeline_id}/runs/{pipeline.run_id}?api-version=7.1"
            )
            try:
                run_data = await self._get(client, run_url)
                items.append(
                    EvidenceItem(
                        type=EvidenceType.PIPELINE,
                        source=self.name,
                        title=f"Pipeline run {pipeline.run_id}",
                        content=run_data,
                        timestamp=datetime.utcnow(),
                        metadata={
                            "state": run_data.get("state"),
                            "result": run_data.get("result"),
                            "url": run_data.get("_links", {}).get("web", {}).get("href"),
                        },
                    )
                )
                # Enrich context
                if not pipeline.commit_sha:
                    pipeline.commit_sha = (
                        run_data.get("resources", {})
                        .get("repositories", {})
                        .get("self", {})
                        .get("version")
                    )
                pipeline.status = run_data.get("state")
                pipeline.result = run_data.get("result")
            except Exception as exc:  # noqa: BLE001
                logger.warning("ado_run_fetch_failed", error=str(exc))

            # Timeline / stages (best-effort)
            timeline_url = (
                f"{pipeline.org_url}/{pipeline.project}/_apis/build/builds/"
                f"{pipeline.run_id}/timeline?api-version=7.1"
            )
            try:
                timeline = await self._get(client, timeline_url)
                failed_records = [
                    r
                    for r in timeline.get("records", [])
                    if r.get("result") in ("failed", "canceled")
                ]
                items.append(
                    EvidenceItem(
                        type=EvidenceType.PIPELINE,
                        source=self.name,
                        title="Failed timeline records",
                        content={"failed_records": failed_records[:50]},
                        metadata={"count": len(failed_records)},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("ado_timeline_skip", error=str(exc))

            # Test results (if published)
            tests_url = (
                f"{pipeline.org_url}/{pipeline.project}/_apis/test/runs"
                f"?buildIds={pipeline.run_id}&api-version=7.1"
            )
            try:
                tests_data = await self._get(client, tests_url)
                for run in tests_data.get("value", [])[:5]:
                    run_id = run.get("id")
                    results_url = (
                        f"{pipeline.org_url}/{pipeline.project}/_apis/test/Runs/"
                        f"{run_id}/results?outcomes=Failed&api-version=7.1"
                    )
                    results = await self._get(client, results_url)
                    for tr in results.get("value", [])[:100]:
                        failure = TestFailureInfo(
                            test_id=str(tr.get("id", "")),
                            test_title=tr.get("automatedTestName") or tr.get("testCaseTitle", "unknown"),
                            error_message=tr.get("errorMessage") or "",
                            error_stack=tr.get("stackTrace"),
                            duration_ms=tr.get("durationInMs"),
                            browser=tr.get("computerName"),
                        )
                        context.failed_tests.append(failure)
                        items.append(
                            EvidenceItem(
                                type=EvidenceType.OTHER,
                                source=self.name,
                                title=f"Failed test: {failure.test_title}",
                                content=failure.model_dump(),
                                related_entities=[failure.test_title],
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                logger.debug("ado_test_results_skip", error=str(exc))

        return EvidenceBundle(collector=self.name, items=items)
