# Architecture — AI Root Cause Investigator

## Design Principles

1. **Evidence over intuition** — every hypothesis must cite supporting evidence items.
2. **Human-in-the-loop** — no automatic code/test changes without explicit approval.
3. **Plugin-first** — new data sources (GitHub Actions, Jenkins, Datadog, Sentry, etc.) plug in without core changes.
4. **Deterministic core + optional LLM** — rule engine always runs; LLM only synthesizes when keys are present.
5. **Time-boxed** — hard timeout (default 120s) so investigations never hang pipelines.
6. **Explainable** — full reasoning trace and impact graph for every conclusion.
7. **Learnable** — memory layer stores closed cases for future similarity search.

## Component Map

```
┌─────────────────────────────────────────────────────────────┐
│                        API / CLI                            │
│              (FastAPI + Typer, JWT auth)                    │
└──────────────────────────┬──────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────┐
│              InvestigationOrchestrator                      │
│         (LangGraph StateGraph or sequential fallback)       │
└─┬──────────┬──────────┬──────────┬──────────┬───────────────┘
  │          │          │          │          │
  ▼          ▼          ▼          ▼          ▼
Collect   Correlate  Reason   Approve    Persist
Evidence  Impact     Root     Human      Memory
(plugins) Graph      Causes   Gate
```

## Plugins

| Plugin                 | Responsibility                                      |
|------------------------|-----------------------------------------------------|
| `azure_devops`         | Pipeline run, timeline, published test results      |
| `git`                  | Recent commits, diffs, authors                      |
| `playwright_artifacts` | Traces, screenshots, videos, console, network       |
| `history`              | Similar past cases from vector memory               |

Extend by subclassing `BaseCollector` / `BaseAnalyzer` and registering with `registry`.

## Security

- Secrets only via environment / secret manager.
- API JWT with short TTL.
- `ALLOW_AUTO_REMEDIATION=false` by default.
- Audit log of every approval decision (structlog JSON).
- Least-privilege PATs recommended for ADO and Git.

## Scalability

- Stateless API nodes; investigation state can be externalized to Redis/Postgres.
- Collectors run concurrently (`asyncio.gather`).
- Vector store (Chroma) can be swapped for Pinecone / Weaviate / Azure AI Search.
- Horizontal scale behind any load balancer; sticky sessions not required if state is shared.

## Observability

- Structured logs (structlog → JSON in production).
- OpenTelemetry hooks ready (`OTEL_EXPORTER_OTLP_ENDPOINT`).
- Prometheus-ready process metrics (optional instrumentation).

## Extension Roadmap

- GitHub Actions / GitLab CI collectors
- Sentry / Datadog / Elastic log correlation
- Automatic Playwright codegen suggestions
- PR comment bot (post findings on the failing PR)
- Multi-repo monorepo impact analysis
