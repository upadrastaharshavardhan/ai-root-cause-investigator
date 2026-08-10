"""CLI entrypoint for AI Root Cause Investigator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional
from uuid import UUID

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from src.config.settings import get_settings
from src.models.schemas import (
    ApprovalDecision,
    InvestigationContext,
    PipelineContext,
    TestFailureInfo,
)
from src.orchestration.langgraph_workflow import InvestigationOrchestrator
from src.utils.logging import setup_logging

app = typer.Typer(
    name="rci",
    help="AI Root Cause Investigator — senior QA engineer in a box",
    add_completion=False,
)
console = Console()


@app.command()
def investigate(
    pipeline_id: int = typer.Option(..., help="Azure DevOps pipeline ID"),
    run_id: int = typer.Option(..., help="Pipeline run ID"),
    project: str = typer.Option(..., help="ADO project name"),
    org: str = typer.Option(None, help="ADO org URL (default from env)"),
    commit: Optional[str] = typer.Option(None, help="Commit SHA override"),
    branch: Optional[str] = typer.Option(None, help="Branch name"),
    timeout: int = typer.Option(120, help="Max investigation seconds"),
    approve: bool = typer.Option(False, help="Auto-approve primary hypothesis (demo only)"),
    output: Optional[Path] = typer.Option(None, help="Write full JSON result to file"),
) -> None:
    """Run a full investigation and print findings."""
    setup_logging()
    settings = get_settings()
    org_url = org or settings.azure_devops_org_url
    if not org_url:
        console.print("[red]org URL required ( --org or AZURE_DEVOPS_ORG_URL )[/red]")
        raise typer.Exit(1)

    pipeline = PipelineContext(
        org_url=org_url,
        project=project,
        pipeline_id=pipeline_id,
        run_id=run_id,
        branch=branch,
        commit_sha=commit,
    )
    context = InvestigationContext(pipeline=pipeline, timeout_seconds=timeout)

    console.print(Panel.fit(
        f"[bold]Investigation[/bold] pipeline={pipeline_id} run={run_id}\n"
        f"id={context.investigation_id}",
        title="RCI",
    ))

    orchestrator = InvestigationOrchestrator()

    async def _run() -> None:
        result = await orchestrator.run(context, approval=None)
        _print_result(result)

        if approve and result.primary_root_cause:
            decision = ApprovalDecision(
                investigation_id=result.investigation_id,
                decision="approve",
                comment="CLI auto-approve",
                decided_by="cli-user",
                selected_hypothesis_ids=[result.primary_root_cause.id],
            )
            # Re-apply approval
            result.approval_status = "approve"
            result.approved_by = "cli-user"
            result.status = result.status  # keep
            from src.models.schemas import InvestigationStatus
            result.status = InvestigationStatus.COMPLETED
            console.print("[green]Approved via --approve flag[/green]")

        if output:
            output.write_text(result.model_dump_json(indent=2))
            console.print(f"[dim]Wrote {output}[/dim]")

    asyncio.run(_run())


@app.command("demo")
def demo(
    with_llm: bool = typer.Option(False, help="Attempt LLM synthesis if keys present"),
) -> None:
    """Run a fully offline demo with synthetic failure evidence."""
    setup_logging()
    console.print(Panel.fit("[bold cyan]Offline demo mode[/bold cyan] — no external services required"))

    pipeline = PipelineContext(
        org_url="https://dev.azure.com/demo",
        project="DemoProject",
        pipeline_id=1,
        run_id=100,
        branch="main",
        commit_sha="abc123def456",
        result="failed",
    )
    context = InvestigationContext(
        pipeline=pipeline,
        failed_tests=[
            TestFailureInfo(
                test_id="t1",
                test_title="checkout should complete purchase",
                file_path="tests/e2e/checkout.spec.ts",
                error_message=(
                    "Timeout 30000ms exceeded.\n"
                    "waiting for locator('button[data-testid=\"pay-now\"]') to be visible\n"
                    "Locator: getByTestId('pay-now')"
                ),
                error_stack="at CheckoutPage.submit (/pages/checkout.ts:42)\nat tests/e2e/checkout.spec.ts:88",
                console_errors=["TypeError: Cannot read properties of undefined (reading 'total')"],
                network_failures=[
                    {"url": "/api/cart/total", "status": 500, "method": "GET"},
                ],
                screenshot_uris=["./data/samples/checkout-fail.png"],
                trace_uri="./data/samples/trace.zip",
            )
        ],
        timeout_seconds=60,
    )

    # Seed a couple of synthetic commits via context (Git collector may no-op without repo)
    from datetime import datetime
    from src.models.schemas import ChangedFile, CommitInfo

    context.recent_commits = [
        CommitInfo(
            sha="abc123def4567890",
            short_sha="abc123de",
            message="refactor: rename pay button test id to confirm-payment",
            author="alice@example.com",
            author_email="alice@example.com",
            committed_at=datetime.utcnow(),
            files=[
                ChangedFile(
                    path="src/components/PayButton.tsx",
                    change_type="modified",
                    additions=3,
                    deletions=3,
                    patch='- data-testid="pay-now"\n+ data-testid="confirm-payment"',
                ),
                ChangedFile(
                    path="src/api/cart.ts",
                    change_type="modified",
                    additions=12,
                    deletions=2,
                ),
            ],
        ),
        CommitInfo(
            sha="def456abc7890123",
            short_sha="def456ab",
            message="fix: handle null cart total",
            author="bob@example.com",
            author_email="bob@example.com",
            committed_at=datetime.utcnow(),
            files=[
                ChangedFile(path="src/api/cart.ts", change_type="modified", additions=8, deletions=1),
            ],
        ),
    ]

    orchestrator = InvestigationOrchestrator()

    async def _run() -> None:
        result = await orchestrator.run(context)
        _print_result(result)

    asyncio.run(_run())


def _print_result(result) -> None:
    table = Table(title="Root Cause Hypotheses", show_lines=True)
    table.add_column("#", style="dim")
    table.add_column("Title")
    table.add_column("Category")
    table.add_column("Confidence")
    table.add_column("Severity")

    for i, h in enumerate(result.hypotheses, 1):
        table.add_row(
            str(i),
            h.title,
            h.category,
            f"{h.confidence:.2f} ({h.confidence_level.value})",
            h.severity.value,
        )
    console.print(table)

    if result.primary_root_cause:
        p = result.primary_root_cause
        console.print(Panel(
            f"[bold]{p.title}[/bold]\n\n{p.description}\n\n"
            f"[green]Fix:[/green] {p.suggested_fix}\n"
            f"[dim]Commits: {', '.join(p.related_commits) or 'n/a'} | "
            f"Devs: {', '.join(p.related_developers) or 'n/a'}[/dim]",
            title="Primary Root Cause",
            border_style="green",
        ))

    if result.explainability_report:
        console.print(Markdown(result.explainability_report))

    console.print(
        f"\n[dim]Status: {result.status.value} | Evidence items: {result.evidence_count} | "
        f"Duration: {result.duration_ms or 'n/a'} ms | ID: {result.investigation_id}[/dim]"
    )


@app.command()
def version() -> None:
    console.print("ai-root-cause-investigator 1.0.0")


if __name__ == "__main__":
    app()
