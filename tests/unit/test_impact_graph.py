"""Tests for impact graph builder."""

from __future__ import annotations

from datetime import datetime

from src.core.impact_graph import ImpactGraphBuilder
from src.models.schemas import (
    ChangedFile,
    CommitInfo,
    InvestigationContext,
    PipelineContext,
    TestFailureInfo,
)


def test_graph_builds_and_ranks() -> None:
    ctx = InvestigationContext(
        pipeline=PipelineContext(
            org_url="https://dev.azure.com/demo",
            project="Demo",
            pipeline_id=1,
            run_id=1,
            commit_sha="abc123",
        ),
        recent_commits=[
            CommitInfo(
                sha="abc123000000",
                short_sha="abc12300",
                message="change pay button",
                author="alice",
                author_email="alice@ex.com",
                committed_at=datetime.utcnow(),
                files=[
                    ChangedFile(path="src/PayButton.tsx", change_type="modified", additions=2, deletions=2),
                ],
            )
        ],
        failed_tests=[
            TestFailureInfo(
                test_id="t1",
                test_title="pay button should work",
                file_path="tests/pay.spec.ts",
                error_message="locator timeout",
            )
        ],
    )
    graph = ImpactGraphBuilder().build(ctx, [])
    assert len(graph.nodes) >= 4
    assert len(graph.edges) >= 2
    assert len(graph.ranked_root_candidates) > 0
    assert any(n.startswith("file:") for n in graph.ranked_root_candidates)
