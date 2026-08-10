"""Core domain models for the AI Root Cause Investigator."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl, field_validator


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class EvidenceType(str, Enum):
    PIPELINE = "pipeline"
    GIT_COMMIT = "git_commit"
    PULL_REQUEST = "pull_request"
    PLAYWRIGHT_TRACE = "playwright_trace"
    SCREENSHOT = "screenshot"
    VIDEO = "video"
    CONSOLE_LOG = "console_log"
    NETWORK_REQUEST = "network_request"
    BACKEND_LOG = "backend_log"
    DATABASE_CHANGE = "database_change"
    HISTORICAL_FAILURE = "historical_failure"
    DOM_SNAPSHOT = "dom_snapshot"
    LOCATOR = "locator"
    ENVIRONMENT = "environment"
    OTHER = "other"


class InvestigationStatus(str, Enum):
    PENDING = "pending"
    COLLECTING = "collecting"
    CORRELATING = "correlating"
    ANALYZING = "analyzing"
    REASONING = "reasoning"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    REMEDIATING = "remediating"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ConfidenceLevel(str, Enum):
    VERY_HIGH = "very_high"  # >= 0.9
    HIGH = "high"            # >= 0.75
    MEDIUM = "medium"        # >= 0.5
    LOW = "low"              # >= 0.3
    SPECULATIVE = "speculative"  # < 0.3


class EvidenceItem(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    type: EvidenceType
    source: str
    title: str
    content: str | dict[str, Any]
    raw_uri: Optional[str] = None
    timestamp: Optional[datetime] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    related_entities: list[str] = Field(default_factory=list)  # file paths, test names, commit shas, etc.


class EvidenceBundle(BaseModel):
    collector: str
    collected_at: datetime = Field(default_factory=datetime.utcnow)
    items: list[EvidenceItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class ChangedFile(BaseModel):
    path: str
    change_type: str  # added | modified | deleted | renamed
    additions: int = 0
    deletions: int = 0
    patch: Optional[str] = None


class CommitInfo(BaseModel):
    sha: str
    short_sha: str
    message: str
    author: str
    author_email: str
    committed_at: datetime
    files: list[ChangedFile] = Field(default_factory=list)
    pr_id: Optional[int] = None
    pr_title: Optional[str] = None
    url: Optional[str] = None


class TestFailureInfo(BaseModel):
    test_id: str
    test_title: str
    suite: Optional[str] = None
    file_path: Optional[str] = None
    error_message: str
    error_stack: Optional[str] = None
    duration_ms: Optional[int] = None
    retries: int = 0
    browser: Optional[str] = None
    project: Optional[str] = None
    annotations: list[str] = Field(default_factory=list)
    screenshot_uris: list[str] = Field(default_factory=list)
    video_uri: Optional[str] = None
    trace_uri: Optional[str] = None
    console_errors: list[str] = Field(default_factory=list)
    network_failures: list[dict[str, Any]] = Field(default_factory=list)


class PipelineContext(BaseModel):
    org_url: str
    project: str
    pipeline_id: int
    run_id: int
    build_id: Optional[int] = None
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    triggered_by: Optional[str] = None
    status: Optional[str] = None
    result: Optional[str] = None
    start_time: Optional[datetime] = None
    finish_time: Optional[datetime] = None
    logs_uri: Optional[str] = None
    artifact_uris: list[str] = Field(default_factory=list)


class InvestigationContext(BaseModel):
    investigation_id: UUID = Field(default_factory=uuid4)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    pipeline: PipelineContext
    failed_tests: list[TestFailureInfo] = Field(default_factory=list)
    recent_commits: list[CommitInfo] = Field(default_factory=list)
    evidence_bundles: list[EvidenceBundle] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 120


class RootCauseHypothesis(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    description: str
    category: str  # locator_shift | api_contract | timeout | flaky | env | code_bug | data | other
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_level: ConfidenceLevel
    severity: Severity
    supporting_evidence_ids: list[UUID] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    affected_tests: list[str] = Field(default_factory=list)
    related_commits: list[str] = Field(default_factory=list)
    related_developers: list[str] = Field(default_factory=list)
    suggested_fix: str
    suggested_fix_steps: list[str] = Field(default_factory=list)
    requires_code_change: bool = False
    requires_test_update: bool = False
    requires_infra_change: bool = False
    reasoning_trace: str = ""
    impact_score: float = 0.0

    @field_validator("confidence_level", mode="before")
    @classmethod
    def derive_confidence_level(cls, v: Any, info: Any) -> ConfidenceLevel:
        if isinstance(v, ConfidenceLevel):
            return v
        conf = info.data.get("confidence", 0.0) if hasattr(info, "data") else 0.0
        if conf >= 0.9:
            return ConfidenceLevel.VERY_HIGH
        if conf >= 0.75:
            return ConfidenceLevel.HIGH
        if conf >= 0.5:
            return ConfidenceLevel.MEDIUM
        if conf >= 0.3:
            return ConfidenceLevel.LOW
        return ConfidenceLevel.SPECULATIVE


class ImpactNode(BaseModel):
    id: str
    label: str
    node_type: str  # commit | file | test | module | developer | evidence
    weight: float = 1.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImpactEdge(BaseModel):
    source: str
    target: str
    relation: str  # modified | covers | failed_on | authored | supports
    weight: float = 1.0


class ImpactGraph(BaseModel):
    nodes: list[ImpactNode] = Field(default_factory=list)
    edges: list[ImpactEdge] = Field(default_factory=list)
    ranked_root_candidates: list[str] = Field(default_factory=list)


class InvestigationResult(BaseModel):
    investigation_id: UUID
    status: InvestigationStatus
    started_at: datetime
    finished_at: Optional[datetime] = None
    duration_ms: Optional[int] = None
    context_summary: dict[str, Any] = Field(default_factory=dict)
    hypotheses: list[RootCauseHypothesis] = Field(default_factory=list)
    primary_root_cause: Optional[RootCauseHypothesis] = None
    impact_graph: Optional[ImpactGraph] = None
    evidence_count: int = 0
    human_approval_required: bool = True
    approval_status: Optional[str] = None  # pending | approved | rejected
    approval_comment: Optional[str] = None
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    remediation_plan: Optional[dict[str, Any]] = None
    memory_case_id: Optional[str] = None
    errors: list[str] = Field(default_factory=list)
    explainability_report: str = ""


class ApprovalRequest(BaseModel):
    investigation_id: UUID
    hypotheses: list[RootCauseHypothesis]
    primary: Optional[RootCauseHypothesis]
    impact_summary: str
    asked_at: datetime = Field(default_factory=datetime.utcnow)


class ApprovalDecision(BaseModel):
    investigation_id: UUID
    decision: str  # approve | reject | request_more_info
    comment: Optional[str] = None
    decided_by: str
    decided_at: datetime = Field(default_factory=datetime.utcnow)
    selected_hypothesis_ids: list[UUID] = Field(default_factory=list)


class InvestigationRequest(BaseModel):
    org_url: str
    project: str
    pipeline_id: int
    run_id: int
    branch: Optional[str] = None
    commit_sha: Optional[str] = None
    failed_test_ids: list[str] = Field(default_factory=list)
    timeout_seconds: int = 120
    include_historical: bool = True
    max_commits_lookback: int = 20
    extra: dict[str, Any] = Field(default_factory=dict)
