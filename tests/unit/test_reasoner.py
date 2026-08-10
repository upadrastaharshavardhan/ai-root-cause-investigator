"""Unit tests for deterministic rule-based reasoning."""

from __future__ import annotations

import pytest

from src.core.reasoner import RootCauseReasoner
from src.models.schemas import (
    EvidenceBundle,
    ImpactGraph,
    InvestigationContext,
    PipelineContext,
    TestFailureInfo,
)


@pytest.fixture
def locator_context() -> InvestigationContext:
    return InvestigationContext(
        pipeline=PipelineContext(
            org_url="https://dev.azure.com/demo",
            project="Demo",
            pipeline_id=1,
            run_id=1,
        ),
        failed_tests=[
            TestFailureInfo(
                test_id="1",
                test_title="login button click",
                error_message="Timeout 30000ms exceeded waiting for locator('button#login')",
                error_stack="at login.spec.ts:20",
            )
        ],
    )


@pytest.mark.asyncio
async def test_locator_rule_fires(locator_context: InvestigationContext) -> None:
    reasoner = RootCauseReasoner()
    hyps = await reasoner.reason(locator_context, [], ImpactGraph())
    assert any(h.category == "locator_shift" for h in hyps)
    primary = hyps[0]
    assert primary.confidence >= 0.5
    assert primary.requires_test_update is True


@pytest.mark.asyncio
async def test_network_timeout_rule() -> None:
    ctx = InvestigationContext(
        pipeline=PipelineContext(
            org_url="https://dev.azure.com/demo",
            project="Demo",
            pipeline_id=1,
            run_id=1,
        ),
        failed_tests=[
            TestFailureInfo(
                test_id="2",
                test_title="api call",
                error_message="net::ERR_CONNECTION_TIMED_OUT",
                network_failures=[{"url": "/api/x", "error": "ETIMEDOUT"}],
            )
        ],
    )
    reasoner = RootCauseReasoner()
    hyps = await reasoner.reason(ctx, [], ImpactGraph())
    assert any(h.category == "timeout" for h in hyps)
