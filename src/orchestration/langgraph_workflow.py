"""LangGraph orchestration of the investigation workflow."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, TypedDict
from uuid import UUID

from src.config.settings import get_settings
from src.core.impact_graph import ImpactGraphBuilder
from src.core.reasoner import RootCauseReasoner
from src.memory.vector_store import MemoryStore
from src.models.schemas import (
    ApprovalDecision,
    EvidenceBundle,
    ImpactGraph,
    InvestigationContext,
    InvestigationResult,
    InvestigationStatus,
    RootCauseHypothesis,
)
from src.plugins.base import registry
from src.plugins.azure_devops import AzureDevOpsCollector
from src.plugins.git_collector import GitCollector
from src.plugins.history import HistoryCollector
from src.plugins.playwright_artifacts import PlaywrightArtifactsCollector
from src.utils.logging import get_logger

logger = get_logger(__name__)


class GraphState(TypedDict, total=False):
    context: InvestigationContext
    evidence: list[EvidenceBundle]
    impact_graph: ImpactGraph
    hypotheses: list[RootCauseHypothesis]
    result: InvestigationResult
    approval: ApprovalDecision | None
    error: str | None


def _register_default_plugins() -> None:
    if not registry.get_collectors():
        registry.register_collector(AzureDevOpsCollector())
        registry.register_collector(GitCollector())
        registry.register_collector(PlaywrightArtifactsCollector())
        registry.register_collector(HistoryCollector())


async def collect_evidence_node(state: GraphState) -> GraphState:
    context = state["context"]
    context_status = InvestigationStatus.COLLECTING
    logger.info("node_collect_start", investigation_id=str(context.investigation_id))

    _register_default_plugins()
    collectors = registry.get_collectors()
    tasks = [c.safe_collect(context) for c in collectors]
    bundles = await asyncio.gather(*tasks)
    evidence = list(bundles)
    context.evidence_bundles = evidence

    logger.info(
        "node_collect_done",
        bundles=len(evidence),
        items=sum(len(b.items) for b in evidence),
    )
    return {**state, "evidence": evidence}


async def correlate_node(state: GraphState) -> GraphState:
    context = state["context"]
    evidence = state.get("evidence") or []
    logger.info("node_correlate_start")
    builder = ImpactGraphBuilder()
    graph = builder.build(context, evidence)
    logger.info(
        "node_correlate_done",
        nodes=len(graph.nodes),
        edges=len(graph.edges),
        top=graph.ranked_root_candidates[:5],
    )
    return {**state, "impact_graph": graph}


async def reason_node(state: GraphState) -> GraphState:
    context = state["context"]
    evidence = state.get("evidence") or []
    graph = state.get("impact_graph") or ImpactGraph()
    logger.info("node_reason_start")
    reasoner = RootCauseReasoner()
    hypotheses = await reasoner.reason(context, evidence, graph)
    logger.info("node_reason_done", hypotheses=len(hypotheses))
    return {**state, "hypotheses": hypotheses}


async def present_for_approval_node(state: GraphState) -> GraphState:
    context = state["context"]
    hypotheses = state.get("hypotheses") or []
    graph = state.get("impact_graph")
    primary = hypotheses[0] if hypotheses else None

    result = InvestigationResult(
        investigation_id=context.investigation_id,
        status=InvestigationStatus.AWAITING_APPROVAL,
        started_at=context.created_at,
        hypotheses=hypotheses,
        primary_root_cause=primary,
        impact_graph=graph,
        evidence_count=sum(len(b.items) for b in (state.get("evidence") or [])),
        human_approval_required=True,
        approval_status="pending",
        explainability_report=_build_explainability(hypotheses, graph),
    )
    logger.info(
        "node_awaiting_approval",
        investigation_id=str(context.investigation_id),
        primary=primary.title if primary else None,
        confidence=primary.confidence if primary else None,
    )
    return {**state, "result": result}


async def apply_approval_node(state: GraphState) -> GraphState:
    result = state.get("result")
    approval = state.get("approval")
    if not result:
        return state

    if approval is None:
        # Interactive / external approval not yet provided — leave as awaiting
        return state

    result.approval_status = approval.decision
    result.approval_comment = approval.comment
    result.approved_by = approval.decided_by
    result.approved_at = approval.decided_at

    if approval.decision == "approve":
        result.status = InvestigationStatus.APPROVED
        settings = get_settings()
        if settings.allow_auto_remediation:
            result.status = InvestigationStatus.REMEDIATING
            result.remediation_plan = {
                "actions": [
                    {
                        "type": "suggested_fix",
                        "hypothesis_id": str(h.id),
                        "steps": h.suggested_fix_steps,
                    }
                    for h in result.hypotheses
                    if h.id in approval.selected_hypothesis_ids or not approval.selected_hypothesis_ids
                ],
                "note": "Auto-remediation is gated; only plan is generated unless explicitly enabled.",
            }
            result.status = InvestigationStatus.COMPLETED
        else:
            result.status = InvestigationStatus.COMPLETED
    elif approval.decision == "reject":
        result.status = InvestigationStatus.REJECTED
    else:
        result.status = InvestigationStatus.AWAITING_APPROVAL

    result.finished_at = datetime.utcnow()
    result.duration_ms = int((result.finished_at - result.started_at).total_seconds() * 1000)
    return {**state, "result": result}


async def persist_memory_node(state: GraphState) -> GraphState:
    result = state.get("result")
    if not result:
        return state
    if result.status not in (
        InvestigationStatus.COMPLETED,
        InvestigationStatus.APPROVED,
        InvestigationStatus.REJECTED,
    ):
        return state
    try:
        store = MemoryStore()
        case_id = await store.persist_investigation(result)
        result.memory_case_id = case_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_persist_skip", error=str(exc))
    return {**state, "result": result}


def _build_explainability(
    hypotheses: list[RootCauseHypothesis], graph: ImpactGraph | None
) -> str:
    lines = ["# Investigation Explainability Report", ""]
    if not hypotheses:
        lines.append("No high-confidence hypotheses could be formed from available evidence.")
        return "\n".join(lines)

    for i, h in enumerate(hypotheses[:5], 1):
        lines.append(f"## {i}. {h.title} (confidence={h.confidence:.2f}, {h.confidence_level.value})")
        lines.append(f"- Category: {h.category}")
        lines.append(f"- Severity: {h.severity.value}")
        lines.append(f"- Description: {h.description}")
        lines.append(f"- Reasoning: {h.reasoning_trace}")
        lines.append(f"- Suggested fix: {h.suggested_fix}")
        if h.suggested_fix_steps:
            lines.append("- Steps:")
            for s in h.suggested_fix_steps:
                lines.append(f"  - {s}")
        lines.append(f"- Affected modules: {', '.join(h.affected_modules) or 'n/a'}")
        lines.append(f"- Related commits: {', '.join(h.related_commits) or 'n/a'}")
        lines.append(f"- Related developers: {', '.join(h.related_developers) or 'n/a'}")
        lines.append("")

    if graph and graph.ranked_root_candidates:
        lines.append("## Top impact graph candidates")
        for n in graph.ranked_root_candidates[:10]:
            lines.append(f"- {n}")
    return "\n".join(lines)


class InvestigationOrchestrator:
    """
    Thin orchestrator that can run with or without full LangGraph installed.
    When langgraph is present, uses StateGraph; otherwise runs the same nodes sequentially.
    """

    def __init__(self) -> None:
        self.settings = get_settings()

    async def run(
        self,
        context: InvestigationContext,
        approval: ApprovalDecision | None = None,
    ) -> InvestigationResult:
        state: GraphState = {
            "context": context,
            "evidence": [],
            "hypotheses": [],
            "approval": approval,
        }

        timeout = min(context.timeout_seconds, self.settings.max_investigation_timeout_seconds)

        try:
            async with asyncio.timeout(timeout):
                # Prefer LangGraph if available
                try:
                    result = await self._run_langgraph(state)
                except Exception as exc:  # noqa: BLE001
                    logger.info("langgraph_fallback_sequential", reason=str(exc))
                    result = await self._run_sequential(state)
                return result
        except TimeoutError:
            logger.error("investigation_timeout", seconds=timeout)
            return InvestigationResult(
                investigation_id=context.investigation_id,
                status=InvestigationStatus.TIMEOUT,
                started_at=context.created_at,
                finished_at=datetime.utcnow(),
                errors=[f"Investigation exceeded {timeout}s timeout"],
                human_approval_required=True,
            )

    async def _run_sequential(self, state: GraphState) -> InvestigationResult:
        state = await collect_evidence_node(state)
        state = await correlate_node(state)
        state = await reason_node(state)
        state = await present_for_approval_node(state)
        state = await apply_approval_node(state)
        state = await persist_memory_node(state)
        result = state.get("result")
        assert result is not None
        return result

    async def _run_langgraph(self, state: GraphState) -> InvestigationResult:
        from langgraph.graph import END, StateGraph

        workflow = StateGraph(GraphState)
        workflow.add_node("collect", collect_evidence_node)
        workflow.add_node("correlate", correlate_node)
        workflow.add_node("reason", reason_node)
        workflow.add_node("present", present_for_approval_node)
        workflow.add_node("approve", apply_approval_node)
        workflow.add_node("persist", persist_memory_node)

        workflow.set_entry_point("collect")
        workflow.add_edge("collect", "correlate")
        workflow.add_edge("correlate", "reason")
        workflow.add_edge("reason", "present")
        workflow.add_edge("present", "approve")
        workflow.add_edge("approve", "persist")
        workflow.add_edge("persist", END)

        app = workflow.compile()
        final = await app.ainvoke(state)
        result = final.get("result")
        assert result is not None
        return result
