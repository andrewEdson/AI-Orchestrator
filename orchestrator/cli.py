"""
orchestrator CLI — entry point.

Usage:
    orchestrator "build a fullstack app with auth"
    orchestrator "build a REST API" --max-workers 6 --retry-limit 2 --verbose
    orchestrator "build a REST API" --dry-run
    orchestrator "build a REST API" --resume RUN_ID
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from orchestrator.executor import Executor
from orchestrator.planner import Planner
from orchestrator.router import Router
from orchestrator.utils.logger import setup_logger
from orchestrator.utils.state import StateManager
from orchestrator.utils.task_graph import TaskGraph

# `app` handles the primary workflow command (orchestrator "<prompt>").
# `manage` handles utility subcommands (list-runs, show-plan).
# A smart entry-point `cli_entry()` dispatches between them based on argv[1].
app = typer.Typer(
    name="orchestrator",
    help="Multi-agent code generation workflow orchestrator.",
    add_completion=False,
    pretty_exceptions_enable=False,
)

manage = typer.Typer(
    name="orchestrator",
    help="Orchestrator management utilities.",
    add_completion=False,
    pretty_exceptions_enable=False,
)
console = Console()

# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

@app.command()
def main(
    prompt: str = typer.Argument(..., help="High-level task description to orchestrate."),
    max_workers: int = typer.Option(
        4, "--max-workers", "-w", min=1, max=32,
        help="Maximum parallel agent workers per wave.",
    ),
    retry_limit: int = typer.Option(
        3, "--retry-limit", "-r", min=1, max=10,
        help="Number of retries before escalating a failing task.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-d",
        help="Print the task plan without executing any agents.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Enable debug-level logging.",
    ),
    resume: Optional[str] = typer.Option(
        None, "--resume",
        help="Resume an interrupted run by its run ID.",
    ),
    output_dir: str = typer.Option(
        "./outputs", "--output-dir",
        help="Directory for task output files.",
    ),
    log_dir: str = typer.Option(
        "./logs", "--log-dir",
        help="Directory for log and state files.",
    ),
    mock: bool = typer.Option(
        False, "--mock",
        help="Use mock agents (no live CLI calls). Useful for testing.",
        hidden=True,
    ),
    claude_review: bool = typer.Option(
        False, "--claude-review",
        help="Ask Claude to review outputs after each wave.",
    ),
    summary_file: Optional[str] = typer.Option(
        None, "--summary-file",
        help="Write run summary JSON to this path.",
    ),
) -> None:
    """
    Orchestrate a multi-agent code generation workflow.

    The PROMPT is decomposed into parallel execution waves by Claude,
    each task is routed to the most appropriate agent (Copilot or Claude),
    and results are validated after each wave.
    """
    # ------------------------------------------------------------------ Setup
    run_id = resume or f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    if mock:
        import os
        os.environ["CLAUDE_MOCK"] = "1"
        os.environ["COPILOT_MOCK"] = "1"

    log = setup_logger("orchestrator", run_id=run_id, log_dir=log_dir, verbose=verbose)

    console.print(
        Panel.fit(
            f"[bold cyan]Orchestrator[/bold cyan]  run_id=[yellow]{run_id}[/yellow]\n"
            f"[dim]{prompt[:120]}[/dim]",
            border_style="cyan",
        )
    )

    if resume:
        log.info("Resuming run: %s", run_id)

    # ------------------------------------------------------------------ State
    state = StateManager(run_id=run_id, log_dir=log_dir)
    state.set_prompt(prompt)

    # ------------------------------------------------------------------ Plan
    log.info("Step 1/3 — Planning…")
    planner = Planner(
        run_id=run_id,
        log_dir=log_dir,
        verbose=verbose,
        mock=mock,
    )

    try:
        plan = planner.plan(prompt)
    except (RuntimeError, ValueError) as exc:
        console.print(f"[bold red]Planner error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    # ------------------------------------------------------------------ Validate graph
    graph = TaskGraph()
    all_tasks = [t for wave in plan["waves"] for t in wave["tasks"]]
    graph.add_tasks(all_tasks)
    try:
        graph.validate()
    except ValueError as exc:
        console.print(f"[bold red]Task graph error:[/bold red] {exc}")
        raise typer.Exit(code=1) from exc

    state.set_plan(plan["waves"])

    # ------------------------------------------------------------------ Dry-run
    if dry_run:
        _display_dry_run(plan, console)
        raise typer.Exit(code=0)

    # ------------------------------------------------------------------ Execute
    log.info("Step 2/3 — Executing %d wave(s)…", len(plan["waves"]))

    router = Router(verbose=verbose)
    executor = Executor(
        state=state,
        router=router,
        output_dir=output_dir,
        max_workers=max_workers,
        retry_limit=retry_limit,
        dry_run=dry_run,
        verbose=verbose,
        context_doc=plan.get("context_doc", ""),
    )
    executor.validator.use_claude_review = claude_review

    summary = executor.run(plan)

    # ------------------------------------------------------------------ Summary
    log.info("Step 3/3 — Summarising…")
    _display_summary(summary, console)

    if summary_file:
        _write_summary(summary, summary_file, run_id, prompt)
        console.print(f"[dim]Summary written to {summary_file}[/dim]")

    console.print(
        f"\n[dim]State: {state.state_path}[/dim]\n"
        f"[dim]Outputs: {Path(output_dir).resolve()}[/dim]"
    )

    if summary["failed"] > 0:
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _display_dry_run(plan: dict, console: Console) -> None:
    """Pretty-print the task plan without executing."""
    console.print("\n[bold yellow]DRY RUN — task plan:[/bold yellow]")
    for wave in plan["waves"]:
        console.print(f"\n[bold]Wave {wave['wave']}[/bold]")
        for task in wave["tasks"]:
            deps = ", ".join(task.get("dependencies", [])) or "none"
            console.print(
                f"  [cyan]{task['id']}[/cyan]  "
                f"[dim]type={task['type']}  deps=[{deps}][/dim]\n"
                f"    {task['task']}"
            )


def _display_summary(summary: dict, console: Console) -> None:
    """Render a rich table summarising all task outcomes."""
    total = summary["total"]
    completed = summary["completed"]
    failed = summary["failed"]

    status_colour = "green" if failed == 0 else "red"
    console.print(
        f"\n[bold {status_colour}]Run complete:[/bold {status_colour}] "
        f"{completed}/{total} tasks succeeded, {failed} failed.\n"
    )

    table = Table(
        title="Task Summary",
        show_header=True,
        header_style="bold",
        border_style="dim",
    )
    table.add_column("Task ID")
    table.add_column("Status", justify="center")
    table.add_column("Agent", justify="center")
    table.add_column("Attempts", justify="right")
    table.add_column("Duration", justify="right")
    table.add_column("Output File")

    status_styles = {
        "completed": "green",
        "failed": "red",
        "escalated": "yellow",
        "running": "cyan",
        "pending": "dim",
    }

    for tid, info in summary["tasks"].items():
        status = info["status"]
        style = status_styles.get(status, "")
        dur = f"{info['duration']:.1f}s" if info.get("duration") else "—"
        table.add_row(
            tid,
            f"[{style}]{status}[/{style}]" if style else status,
            info.get("agent") or "—",
            str(info.get("attempts", 0)),
            dur,
            info.get("output_file") or "—",
        )

    console.print(table)


def _write_summary(summary: dict, path: str, run_id: str, prompt: str) -> None:
    out = {
        "run_id": run_id,
        "prompt": prompt,
        **summary,
    }
    Path(path).write_text(json.dumps(out, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Sub-commands
# ---------------------------------------------------------------------------

@manage.command("list-runs")
def list_runs(
    log_dir: str = typer.Option("./logs", "--log-dir"),
) -> None:
    """List all previous orchestration runs found in the log directory."""
    log_path = Path(log_dir)
    if not log_path.exists():
        console.print("[dim]No runs found.[/dim]")
        raise typer.Exit()

    state_files = sorted(log_path.glob("*_state.json"), reverse=True)
    if not state_files:
        console.print("[dim]No runs found.[/dim]")
        raise typer.Exit()

    table = Table(title="Previous Runs", header_style="bold cyan")
    table.add_column("Run ID")
    table.add_column("Prompt")
    table.add_column("Tasks", justify="right")
    table.add_column("Completed", justify="right")
    table.add_column("Failed", justify="right")

    for sf in state_files[:20]:  # show up to 20 most recent
        try:
            import json as _json
            data = _json.loads(sf.read_text(encoding="utf-8"))
            tasks = data.get("tasks", {})
            completed = sum(1 for t in tasks.values() if t["status"] == "completed")
            failed = sum(1 for t in tasks.values() if t["status"] == "failed")
            prompt_snippet = data.get("prompt", "")[:60]
            table.add_row(
                data.get("run_id", sf.stem),
                prompt_snippet,
                str(len(tasks)),
                str(completed),
                str(failed),
            )
        except Exception:  # noqa: BLE001
            table.add_row(sf.stem, "[dim]unreadable[/dim]", "?", "?", "?")

    console.print(table)


@manage.command("show-plan")
def show_plan(
    run_id: str = typer.Argument(..., help="Run ID to inspect."),
    log_dir: str = typer.Option("./logs", "--log-dir"),
) -> None:
    """Display the saved task plan for a previous run."""
    plan_file = Path(log_dir) / f"{run_id}_plan.json"
    if not plan_file.exists():
        console.print(f"[red]Plan file not found:[/red] {plan_file}")
        raise typer.Exit(code=1)

    import json as _json
    plan = _json.loads(plan_file.read_text(encoding="utf-8"))
    _display_dry_run(plan, console)


# ---------------------------------------------------------------------------
# Smart entry-point dispatcher
# ---------------------------------------------------------------------------

# Management subcommand names — anything else is treated as the PROMPT
_MANAGE_CMDS = {"list-runs", "show-plan"}


def cli_entry() -> None:
    """
    Top-level entry point registered in pyproject.toml.

    Routes to the management sub-app when argv[1] is a known management
    command; otherwise delegates to the main `app` (which takes a PROMPT).

    This lets both of these work:
        orchestrator "build a REST API"
        orchestrator list-runs
    """
    import sys

    if len(sys.argv) > 1 and sys.argv[1] in _MANAGE_CMDS:
        manage()
    else:
        app()


if __name__ == "__main__":
    cli_entry()
