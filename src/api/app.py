"""FastAPI application for the AI Root Cause Investigator."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from src.config.settings import get_settings
from src.models.schemas import (
    ApprovalDecision,
    InvestigationContext,
    InvestigationRequest,
    InvestigationResult,
    InvestigationStatus,
    PipelineContext,
)
from src.orchestration.langgraph_workflow import InvestigationOrchestrator
from src.utils.logging import get_logger, setup_logging

setup_logging()
logger = get_logger(__name__)
settings = get_settings()

app = FastAPI(
    title="AI Root Cause Investigator",
    description="Enterprise AI Quality OS for automated Playwright / test failure root-cause analysis",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)

# In-memory store for demo / single-node; replace with Redis/Postgres in multi-node
_INVESTIGATIONS: dict[str, InvestigationResult] = {}
_CONTEXTS: dict[str, InvestigationContext] = {}


class TokenRequest(BaseModel):
    client_id: str
    client_secret: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def create_access_token(sub: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": sub, "exp": expire}
    return jwt.encode(
        payload,
        settings.secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Security(security),
) -> str:
    if settings.app_env == "development" and creds is None:
        return "dev-user"
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing token")
    try:
        payload = jwt.decode(
            creds.credentials,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.jwt_algorithm],
        )
        sub: str | None = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Invalid token")
        return sub
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "ai-root-cause-investigator", "version": "1.0.0"}


@app.post("/auth/token", response_model=TokenResponse)
async def issue_token(body: TokenRequest) -> TokenResponse:
    # Production: validate against IdP / service principal
    if body.client_secret != settings.secret_key.get_secret_value() and settings.is_production:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_access_token(body.client_id)
    return TokenResponse(access_token=token, expires_in=settings.jwt_expire_minutes * 60)


@app.post("/investigate", response_model=InvestigationResult)
async def start_investigation(
    req: InvestigationRequest,
    user: str = Depends(get_current_user),
) -> InvestigationResult:
    logger.info("investigation_requested", user=user, pipeline_id=req.pipeline_id, run_id=req.run_id)

    pipeline = PipelineContext(
        org_url=req.org_url,
        project=req.project,
        pipeline_id=req.pipeline_id,
        run_id=req.run_id,
        branch=req.branch,
        commit_sha=req.commit_sha,
    )
    context = InvestigationContext(
        pipeline=pipeline,
        timeout_seconds=req.timeout_seconds,
        extra=req.extra,
    )
    _CONTEXTS[str(context.investigation_id)] = context

    orchestrator = InvestigationOrchestrator()
    # First pass stops at AWAITING_APPROVAL (no approval decision yet)
    result = await orchestrator.run(context, approval=None)
    _INVESTIGATIONS[str(result.investigation_id)] = result
    return result


@app.get("/investigate/{investigation_id}", response_model=InvestigationResult)
async def get_investigation(
    investigation_id: UUID,
    user: str = Depends(get_current_user),
) -> InvestigationResult:
    result = _INVESTIGATIONS.get(str(investigation_id))
    if not result:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return result


@app.post("/investigate/{investigation_id}/approve", response_model=InvestigationResult)
async def approve_investigation(
    investigation_id: UUID,
    decision: ApprovalDecision,
    user: str = Depends(get_current_user),
) -> InvestigationResult:
    if decision.investigation_id != investigation_id:
        raise HTTPException(status_code=400, detail="investigation_id mismatch")

    context = _CONTEXTS.get(str(investigation_id))
    prior = _INVESTIGATIONS.get(str(investigation_id))
    if not context or not prior:
        raise HTTPException(status_code=404, detail="Investigation not found")

    decision.decided_by = user
    orchestrator = InvestigationOrchestrator()
    # Re-run from approval node by supplying decision (sequential path reapplies)
    # For efficiency we only re-apply approval + persist
    prior.approval_status = decision.decision
    prior.approval_comment = decision.comment
    prior.approved_by = user
    prior.approved_at = datetime.utcnow()

    if decision.decision == "approve":
        prior.status = InvestigationStatus.COMPLETED
        if settings.allow_auto_remediation:
            prior.remediation_plan = {
                "note": "Auto-remediation disabled by default; plan only.",
                "selected": [str(i) for i in decision.selected_hypothesis_ids],
            }
    elif decision.decision == "reject":
        prior.status = InvestigationStatus.REJECTED
    else:
        prior.status = InvestigationStatus.AWAITING_APPROVAL

    prior.finished_at = datetime.utcnow()
    prior.duration_ms = int((prior.finished_at - prior.started_at).total_seconds() * 1000)

    try:
        from src.memory.vector_store import MemoryStore

        store = MemoryStore()
        prior.memory_case_id = await store.persist_investigation(prior)
    except Exception as exc:  # noqa: BLE001
        logger.warning("approve_persist_failed", error=str(exc))

    _INVESTIGATIONS[str(investigation_id)] = prior
    logger.info(
        "investigation_decision",
        investigation_id=str(investigation_id),
        decision=decision.decision,
        user=user,
    )
    return prior


@app.get("/investigate/{investigation_id}/report")
async def get_report(
    investigation_id: UUID,
    user: str = Depends(get_current_user),
) -> dict[str, Any]:
    result = _INVESTIGATIONS.get(str(investigation_id))
    if not result:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return {
        "investigation_id": str(result.investigation_id),
        "status": result.status.value,
        "primary_root_cause": result.primary_root_cause.model_dump() if result.primary_root_cause else None,
        "hypotheses": [h.model_dump() for h in result.hypotheses],
        "explainability_report": result.explainability_report,
        "impact_graph_summary": {
            "nodes": len(result.impact_graph.nodes) if result.impact_graph else 0,
            "edges": len(result.impact_graph.edges) if result.impact_graph else 0,
            "top_candidates": result.impact_graph.ranked_root_candidates[:15]
            if result.impact_graph
            else [],
        },
        "evidence_count": result.evidence_count,
        "duration_ms": result.duration_ms,
        "approval_status": result.approval_status,
    }


def run() -> None:
    import uvicorn

    uvicorn.run(
        "src.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=not settings.is_production,
    )


if __name__ == "__main__":
    run()
