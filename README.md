# AI Root Cause Investigator (RCI)

**Enterprise-grade AI Quality Operating System** that behaves like a senior QA engineer with 15+ years of debugging experience.

When a Playwright (or framework-agnostic) test pipeline fails, RCI automatically:

1. Collects & correlates evidence from Azure DevOps, Git, PRs, Playwright traces/screenshots/videos, console & network logs, backend logs, DB changes, and historical failures.
2. Builds an **impact graph** of recent commits, changed files, developers, and modules.
3. Reasons over DOM/locator shifts, API contract changes, timeouts, console errors, flakiness, and environment issues.
4. Produces **evidence-backed** findings with confidence scores, affected modules, and suggested fixes — **no guessing**.
5. Presents findings for **human approval** before any corrective action.
6. Continuously learns via a memory layer (vector store + historical cases).

Investigation target: **under 2 minutes**.

## Architecture Highlights

- **Plugin-based** data collectors & analyzers (Azure DevOps, Git, Playwright artifacts, logs, history, custom).
- **LangGraph** orchestration for deterministic, auditable multi-step reasoning.
- **Impact Graph** (NetworkX) correlating code changes → tests → failures.
- **Memory layer** (ChromaDB + embeddings) for continuous learning from past investigations.
- **Human-in-the-loop** approval gate before auto-remediation.
- **Explainable** outputs with full evidence trails.
- **Enterprise security**: secrets via env/vault, JWT auth for API, audit logging, least-privilege plugins.
- Framework-agnostic core, Playwright-first adapters.
- Observability: structured logs (structlog), Prometheus metrics, OpenTelemetry.

## Quick Start

```bash
# 1. Clone / unzip
cd ai-root-cause-investigator

# 2. Python 3.11+
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env
# Edit .env with Azure DevOps PAT, OpenAI/Anthropic key, Git tokens, etc.

# 4. Run CLI investigation (example)
rci investigate \
  --pipeline-id 12345 \
  --run-id 67890 \
  --project MyProject \
  --org https://dev.azure.com/myorg

# 5. Or start API
rci-api
# Docs at http://localhost:8000/docs
```

Docker:

```bash
docker compose up --build
```

## Core Flow (LangGraph)

```
START
  → CollectEvidence (plugins in parallel)
  → Correlate & Build Impact Graph
  → Analyze Artifacts (trace, network, console, DOM)
  → Reason Root Causes (LLM + rules + memory)
  → Rank & Score Findings
  → Present for Human Approval
  → (optional) Apply Remediation (after approval)
  → Persist to Memory
END
```

## Plugin System

Implement `BaseCollector` or `BaseAnalyzer`:

```python
from src.plugins.base import BaseCollector, EvidenceBundle

class MyCollector(BaseCollector):
    name = "my_source"
    async def collect(self, context: InvestigationContext) -> EvidenceBundle:
        ...
```

Register in `configs/plugins.yaml` or via code.

## Security & Compliance

- No secrets in code; all via environment / Azure Key Vault / HashiCorp Vault adapters.
- API protected by JWT + role-based scopes.
- Full audit trail of every investigation and approval decision.
- Plugins run with least privilege; sandboxable.
- Data retention policies configurable.

## Production Checklist

- [ ] Configure real Azure DevOps, Git, LLM credentials
- [ ] Point Playwright artifact storage (ADO, S3, Azure Blob)
- [ ] Enable vector store persistence (ChromaDB or Pinecone/Weaviate)
- [ ] Set up Prometheus + Grafana dashboards
- [ ] Configure human approval webhook / Slack / Teams channel
- [ ] Run `pytest` and load tests
- [ ] Deploy behind API gateway with mTLS if required

## License

Apache-2.0
