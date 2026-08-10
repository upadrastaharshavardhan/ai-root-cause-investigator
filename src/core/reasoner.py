"""Root-cause reasoning engine: rules + LLM + memory, evidence-backed only."""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID

from src.config.settings import get_settings
from src.models.schemas import (
    ConfidenceLevel,
    EvidenceBundle,
    EvidenceItem,
    ImpactGraph,
    InvestigationContext,
    RootCauseHypothesis,
    Severity,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)


RULE_PATTERNS: list[dict[str, Any]] = [
    {
        "id": "locator_shift",
        "category": "locator_shift",
        "patterns": [
            r"locator\.(click|fill|check|select)",
            r"waiting for (locator|selector)",
            r"strict mode violation",
            r"element is not visible",
            r"Timeout.*locator",
            r"getBy(Role|Text|TestId|Label)",
        ],
        "title": "Possible locator / DOM shift",
        "severity": Severity.HIGH,
        "base_confidence": 0.7,
        "requires_test_update": True,
    },
    {
        "id": "network_timeout",
        "category": "timeout",
        "patterns": [
            r"Timeout\s*\d+ms exceeded",
            r"net::ERR_",
            r"Request failed",
            r"ECONNREFUSED",
            r"ETIMEDOUT",
            r"Navigation timeout",
        ],
        "title": "Network or navigation timeout",
        "severity": Severity.HIGH,
        "base_confidence": 0.75,
        "requires_infra_change": True,
    },
    {
        "id": "api_contract",
        "category": "api_contract",
        "patterns": [
            r"status (4\d\d|5\d\d)",
            r"Unexpected token",
            r"JSON\.parse",
            r"contract|schema|openapi",
            r"response\.status\(\)",
        ],
        "title": "API contract or status code change",
        "severity": Severity.CRITICAL,
        "base_confidence": 0.65,
        "requires_code_change": True,
    },
    {
        "id": "console_error",
        "category": "code_bug",
        "patterns": [
            r"TypeError",
            r"ReferenceError",
            r"Uncaught",
            r"Cannot read propert",
            r"is not a function",
        ],
        "title": "Frontend runtime / console error",
        "severity": Severity.HIGH,
        "base_confidence": 0.7,
        "requires_code_change": True,
    },
    {
        "id": "flaky",
        "category": "flaky",
        "patterns": [
            r"flaky",
            r"retry",
            r"intermittent",
            r"race condition",
        ],
        "title": "Suspected flaky test",
        "severity": Severity.MEDIUM,
        "base_confidence": 0.55,
        "requires_test_update": True,
    },
]


class RootCauseReasoner:
    def __init__(self) -> None:
        self.settings = get_settings()

    async def reason(
        self,
        context: InvestigationContext,
        evidence: list[EvidenceBundle],
        impact_graph: ImpactGraph,
    ) -> list[RootCauseHypothesis]:
        # 1. Deterministic rule-based hypotheses
        rule_hyps = self._apply_rules(context, evidence)

        # 2. Graph-informed boosts
        rule_hyps = self._boost_from_graph(rule_hyps, impact_graph, context)

        # 3. LLM synthesis (optional, falls back gracefully)
        llm_hyps = await self._llm_synthesize(context, evidence, impact_graph, rule_hyps)

        # Merge & dedupe by category+title similarity
        all_hyps = self._merge(rule_hyps + llm_hyps)

        # Sort by confidence * impact
        all_hyps.sort(key=lambda h: (h.confidence * (1 + h.impact_score)), reverse=True)
        return all_hyps[:10]

    def _apply_rules(
        self, context: InvestigationContext, evidence: list[EvidenceBundle]
    ) -> list[RootCauseHypothesis]:
        hyps: list[RootCauseHypothesis] = []
        corpus_parts: list[str] = []
        evidence_ids: list[UUID] = []

        for t in context.failed_tests:
            corpus_parts.append(f"{t.test_title}\n{t.error_message}\n{t.error_stack or ''}")
            for e in t.console_errors:
                corpus_parts.append(str(e))
            for n in t.network_failures:
                corpus_parts.append(json.dumps(n))

        for bundle in evidence:
            for item in bundle.items:
                content = item.content if isinstance(item.content, str) else json.dumps(item.content)[:2000]
                corpus_parts.append(f"{item.title}\n{content}")
                evidence_ids.append(item.id)

        corpus = "\n".join(corpus_parts).lower()

        for rule in RULE_PATTERNS:
            matches = 0
            for pat in rule["patterns"]:
                if re.search(pat, corpus, re.IGNORECASE):
                    matches += 1
            if matches == 0:
                continue

            conf = min(0.95, rule["base_confidence"] + 0.05 * (matches - 1))
            affected_tests = [t.test_title for t in context.failed_tests]
            related_commits = [c.short_sha for c in context.recent_commits[:5]]
            related_devs = list({c.author for c in context.recent_commits[:5]})

            hyps.append(
                RootCauseHypothesis(
                    title=rule["title"],
                    description=(
                        f"Rule '{rule['id']}' matched {matches} pattern(s) in failure evidence. "
                        f"This is a strong signal for {rule['category']}."
                    ),
                    category=rule["category"],
                    confidence=conf,
                    confidence_level=ConfidenceLevel.HIGH if conf >= 0.75 else ConfidenceLevel.MEDIUM,
                    severity=rule["severity"],
                    supporting_evidence_ids=evidence_ids[:20],
                    affected_modules=self._guess_modules(context),
                    affected_tests=affected_tests,
                    related_commits=related_commits,
                    related_developers=related_devs,
                    suggested_fix=self._default_fix(rule["category"]),
                    suggested_fix_steps=self._default_steps(rule["category"]),
                    requires_code_change=rule.get("requires_code_change", False),
                    requires_test_update=rule.get("requires_test_update", False),
                    requires_infra_change=rule.get("requires_infra_change", False),
                    reasoning_trace=f"Deterministic rule match: {rule['id']} (matches={matches})",
                    impact_score=0.5 + 0.1 * matches,
                )
            )
        return hyps

    def _boost_from_graph(
        self,
        hyps: list[RootCauseHypothesis],
        graph: ImpactGraph,
        context: InvestigationContext,
    ) -> list[RootCauseHypothesis]:
        top_files = [n for n in graph.ranked_root_candidates if n.startswith("file:")][:10]
        top_commits = [n for n in graph.ranked_root_candidates if n.startswith("commit:")][:5]
        for h in hyps:
            # If recent commits touch files that rank high, boost
            for c in context.recent_commits[:3]:
                for f in c.files:
                    if f"file:{f.path}" in top_files:
                        h.confidence = min(0.98, h.confidence + 0.08)
                        h.impact_score += 0.3
                        h.related_commits = list(set(h.related_commits + [c.short_sha]))
                        h.reasoning_trace += f" | Graph boost: high-rank file {f.path}"
            if top_commits:
                h.related_commits = list(set(h.related_commits + [c.replace("commit:", "") for c in top_commits[:3]]))
        return hyps

    async def _llm_synthesize(
        self,
        context: InvestigationContext,
        evidence: list[EvidenceBundle],
        graph: ImpactGraph,
        existing: list[RootCauseHypothesis],
    ) -> list[RootCauseHypothesis]:
        settings = self.settings
        if not settings.openai_api_key and not settings.anthropic_api_key and not settings.azure_openai_api_key:
            logger.info("llm_skipped_no_key")
            return []

        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            from langchain_openai import ChatOpenAI
        except ImportError:
            logger.warning("langchain_not_available")
            return []

        # Build compact prompt
        summary = {
            "failed_tests": [
                {"title": t.test_title, "error": t.error_message[:300]} for t in context.failed_tests[:5]
            ],
            "recent_commits": [
                {
                    "sha": c.short_sha,
                    "msg": c.message,
                    "author": c.author,
                    "files": [f.path for f in c.files[:8]],
                }
                for c in context.recent_commits[:8]
            ],
            "top_impact_nodes": graph.ranked_root_candidates[:15],
            "existing_hypotheses": [h.title for h in existing],
            "evidence_titles": [
                item.title for b in evidence for item in b.items[:20]
            ],
        }

        system = (
            "You are a principal QA architect with 15+ years of experience diagnosing Playwright "
            "and end-to-end test failures. You ONLY propose root causes that are supported by the "
            "provided evidence. Never invent commits, files, or errors that are not present. "
            "Respond with a JSON array of objects with keys: "
            "title, description, category, confidence (0-1), severity, suggested_fix, "
            "suggested_fix_steps (array), requires_code_change, requires_test_update, "
            "requires_infra_change, reasoning_trace, affected_modules (array)."
        )
        human = (
            "Investigate this failure. Use only the evidence below.\n\n"
            f"```json\n{json.dumps(summary, indent=2)[:12000]}\n```\n\n"
            "Return at most 3 high-quality, evidence-backed hypotheses as pure JSON array."
        )

        try:
            model_name = settings.openai_model
            llm = ChatOpenAI(
                model=model_name,
                api_key=settings.openai_api_key.get_secret_value() if settings.openai_api_key else None,
                temperature=0.1,
            )
            resp = await llm.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=human)]
            )
            text = resp.content if isinstance(resp.content, str) else str(resp.content)
            # Extract JSON
            match = re.search(r"\[[\s\S]*\]", text)
            if not match:
                return []
            raw = json.loads(match.group(0))
            hyps: list[RootCauseHypothesis] = []
            for item in raw[:3]:
                conf = float(item.get("confidence", 0.5))
                hyps.append(
                    RootCauseHypothesis(
                        title=item.get("title", "LLM hypothesis"),
                        description=item.get("description", ""),
                        category=item.get("category", "other"),
                        confidence=conf,
                        confidence_level=ConfidenceLevel.MEDIUM,
                        severity=Severity(item.get("severity", "medium")),
                        supporting_evidence_ids=[],
                        affected_modules=item.get("affected_modules", []),
                        affected_tests=[t.test_title for t in context.failed_tests],
                        related_commits=[c.short_sha for c in context.recent_commits[:5]],
                        related_developers=list({c.author for c in context.recent_commits[:5]}),
                        suggested_fix=item.get("suggested_fix", ""),
                        suggested_fix_steps=item.get("suggested_fix_steps", []),
                        requires_code_change=bool(item.get("requires_code_change")),
                        requires_test_update=bool(item.get("requires_test_update")),
                        requires_infra_change=bool(item.get("requires_infra_change")),
                        reasoning_trace=item.get("reasoning_trace", "LLM synthesis"),
                        impact_score=0.6,
                    )
                )
            return hyps
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_reason_failed", error=str(exc))
            return []

    def _merge(self, hyps: list[RootCauseHypothesis]) -> list[RootCauseHypothesis]:
        seen: dict[str, RootCauseHypothesis] = {}
        for h in hyps:
            key = f"{h.category}:{h.title[:40].lower()}"
            if key not in seen or h.confidence > seen[key].confidence:
                seen[key] = h
        return list(seen.values())

    def _guess_modules(self, context: InvestigationContext) -> list[str]:
        modules: set[str] = set()
        for c in context.recent_commits:
            for f in c.files:
                parts = f.path.replace("\\", "/").split("/")
                if parts:
                    modules.add(parts[0])
        return list(modules)[:10]

    def _default_fix(self, category: str) -> str:
        return {
            "locator_shift": "Update Playwright locators to match current DOM; prefer getByRole/getByTestId.",
            "timeout": "Increase timeout only if justified; prefer fixing slow backend or network conditions.",
            "api_contract": "Align test expectations with latest API contract; update mocks/fixtures.",
            "code_bug": "Fix the runtime error in application code referenced by console/stack.",
            "flaky": "Stabilize test: remove race conditions, add proper auto-waiting, isolate data.",
            "env": "Verify environment configuration, secrets, and feature flags.",
        }.get(category, "Investigate supporting evidence and apply targeted fix.")

    def _default_steps(self, category: str) -> list[str]:
        return {
            "locator_shift": [
                "Open the Playwright trace and inspect the failing action.",
                "Compare previous vs current DOM for the target element.",
                "Replace brittle CSS/XPath with role/text/test-id locators.",
                "Re-run the single test with --debug.",
            ],
            "timeout": [
                "Check network waterfall in the trace for slow or failed requests.",
                "Correlate with backend latency / error logs for the same timestamp.",
                "Confirm environment capacity and any recent infra changes.",
            ],
            "api_contract": [
                "Diff recent commits that touch API handlers or OpenAPI specs.",
                "Update test assertions and fixtures to the new contract.",
                "Add contract tests if missing.",
            ],
        }.get(
            category,
            [
                "Review ranked impact graph nodes.",
                "Inspect supporting evidence items.",
                "Propose a minimal, reversible change.",
            ],
        )
