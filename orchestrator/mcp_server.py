"""
MCP server — exposes the orchestrator as tools that Claude Code can call.

Once registered in Claude Code's settings, Claude can autonomously decide
to invoke these tools rather than you having to run the CLI manually.

Tools exposed:
    run_workflow       — decompose + execute a full orchestration run
    dry_run_workflow   — return the task plan without executing
    get_run_status     — inspect a specific run's state
    list_runs          — list all previous runs
    resume_run         — continue an interrupted run

Start the server:
    python -m orchestrator.mcp_server
    # or via the installed entry point:
    orchestrator-mcp
"""

from __future__ import annotations

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "orchestrator",
    instructions=(
        "Use 'run_workflow' when the user asks you to build, generate, or create "
        "a substantial software project. Use 'dry_run_workflow' first if you want "
        "to preview the task breakdown before committing to a full run."
    ),
)

# Thread pool for running the synchronous orchestrator engine without
# blocking FastMCP's async event loop.
_thread_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="orch-worker")

# Default paths — can be overridden via env vars
_OUTPUT_DIR = os.environ.get("ORCHESTRATOR_OUTPUT_DIR", "./outputs")
_LOG_DIR = os.environ.get("ORCHESTRATOR_LOG_DIR", "./logs")
_WORKDIR = os.environ.get("ORCHESTRATOR_WORKDIR", str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool
def run_workflow(
    prompt: str,
    max_workers: int = 4,
    retry_limit: int = 3,
    mock: bool = False,
) -> str:
    """
    Decompose a high-level prompt into tasks and execute them in parallel waves.

    Uses Claude as the planner to break the prompt into structured waves, routes
    each task to the best agent (Copilot for simple tasks, Claude for complex),
    runs tasks in parallel within each wave, validates outputs, and retries or
    escalates failures.

    Args:
        prompt:      The software project or task to build.
        max_workers: Max parallel agent tasks per wave (default 4).
        retry_limit: Retries before escalating a failing task to Claude (default 3).
        mock:        Use mock agents — no live CLI calls (useful for testing).

    Returns:
        A JSON string containing the run summary with task statuses and output paths.
    """
    if mock:
        os.environ["CLAUDE_MOCK"] = "1"
        os.environ["COPILOT_MOCK"] = "1"

    return _run_in_workdir(_execute_workflow, prompt, max_workers, retry_limit, False)


@mcp.tool
def dry_run_workflow(prompt: str) -> str:
    """
    Generate and return the task plan for a prompt without executing anything.

    Useful for previewing how the orchestrator would decompose a request into
    waves before committing to a full run.

    Args:
        prompt: The software project or task to plan.

    Returns:
        A formatted string showing the wave-by-wave task breakdown.
    """
    return _run_in_workdir(_execute_workflow, prompt, 4, 3, True)


@mcp.tool
def get_run_status(run_id: str) -> str:
    """
    Return the current status of a specific orchestration run.

    Args:
        run_id: The run ID returned by run_workflow (e.g. run_1711234567_abc123).

    Returns:
        JSON string with task statuses, agents used, attempt counts, and output paths.
    """
    state_file = Path(_LOG_DIR) / f"{run_id}_state.json"
    if not state_file.exists():
        return json.dumps({"error": f"No state file found for run_id '{run_id}'"})

    data = json.loads(state_file.read_text(encoding="utf-8"))
    tasks = data.get("tasks", {})

    status_counts = {"completed": 0, "failed": 0, "running": 0, "pending": 0}
    for t in tasks.values():
        s = t.get("status", "pending")
        status_counts[s] = status_counts.get(s, 0) + 1

    return json.dumps(
        {
            "run_id": run_id,
            "prompt": data.get("prompt", ""),
            "counts": status_counts,
            "tasks": {
                tid: {
                    "status": t["status"],
                    "agent": t.get("agent"),
                    "attempts": t.get("attempts", 0),
                    "output_file": t.get("output_file"),
                    "error": t.get("error"),
                }
                for tid, t in tasks.items()
            },
        },
        indent=2,
    )


@mcp.tool
def list_runs(limit: int = 10) -> str:
    """
    List previous orchestration runs, most recent first.

    Args:
        limit: Maximum number of runs to return (default 10).

    Returns:
        JSON array of run summaries including prompt, task counts, and status.
    """
    log_path = Path(_LOG_DIR)
    if not log_path.exists():
        return json.dumps([])

    state_files = sorted(log_path.glob("*_state.json"), reverse=True)[:limit]
    runs = []

    for sf in state_files:
        try:
            data = json.loads(sf.read_text(encoding="utf-8"))
            tasks = data.get("tasks", {})
            completed = sum(1 for t in tasks.values() if t["status"] == "completed")
            failed = sum(1 for t in tasks.values() if t["status"] == "failed")
            runs.append(
                {
                    "run_id": data.get("run_id", sf.stem.replace("_state", "")),
                    "prompt": data.get("prompt", "")[:80],
                    "total_tasks": len(tasks),
                    "completed": completed,
                    "failed": failed,
                    "updated_at": data.get("updated_at"),
                }
            )
        except Exception:  # noqa: BLE001
            runs.append({"run_id": sf.stem, "error": "unreadable"})

    return json.dumps(runs, indent=2)


@mcp.tool
def resume_run(run_id: str, prompt: str, max_workers: int = 4, retry_limit: int = 3) -> str:
    """
    Resume an interrupted orchestration run from where it left off.

    Completed tasks are skipped; only pending or failed tasks are re-executed.

    Args:
        run_id:      The run ID of the interrupted run.
        prompt:      The original prompt (must match the original run).
        max_workers: Max parallel workers (default 4).
        retry_limit: Retry limit for failing tasks (default 3).

    Returns:
        JSON string with the final run summary.
    """
    return _run_in_workdir(_resume_workflow, run_id, prompt, max_workers, retry_limit)


# ---------------------------------------------------------------------------
# Internal helpers — these run in the thread pool
# ---------------------------------------------------------------------------

def _run_in_workdir(fn, *args, **kwargs) -> str:
    """Change to the project workdir before running fn (important for relative paths)."""
    original_dir = os.getcwd()
    try:
        os.chdir(_WORKDIR)
        return fn(*args, **kwargs)
    finally:
        os.chdir(original_dir)


def _execute_workflow(
    prompt: str,
    max_workers: int,
    retry_limit: int,
    dry_run: bool,
) -> str:
    """Core workflow execution — runs synchronously in a thread."""
    from orchestrator.executor import Executor
    from orchestrator.planner import Planner
    from orchestrator.router import Router
    from orchestrator.utils.state import StateManager
    from orchestrator.utils.task_graph import TaskGraph

    run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"

    state = StateManager(run_id=run_id, log_dir=_LOG_DIR)
    state.set_prompt(prompt)

    planner = Planner(run_id=run_id, log_dir=_LOG_DIR)
    try:
        plan = planner.plan(prompt)
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": f"Planner failed: {exc}"})

    # Validate dependency graph
    graph = TaskGraph()
    all_tasks = [t for wave in plan["waves"] for t in wave["tasks"]]
    graph.add_tasks(all_tasks)
    try:
        graph.validate()
    except ValueError as exc:
        return json.dumps({"error": f"Task graph invalid: {exc}"})

    state.set_plan(plan["waves"])

    if dry_run:
        return _format_dry_run(plan)

    router = Router()
    executor = Executor(
        state=state,
        router=router,
        output_dir=_OUTPUT_DIR,
        max_workers=max_workers,
        retry_limit=retry_limit,
    )
    summary = executor.run(plan)
    summary["run_id"] = run_id
    return json.dumps(summary, indent=2)


def _resume_workflow(
    run_id: str,
    prompt: str,
    max_workers: int,
    retry_limit: int,
) -> str:
    """Resume an existing run — same as _execute_workflow but reuses the existing run_id."""
    from orchestrator.executor import Executor
    from orchestrator.planner import Planner
    from orchestrator.router import Router
    from orchestrator.utils.state import StateManager

    state = StateManager(run_id=run_id, log_dir=_LOG_DIR)
    state.set_prompt(prompt)

    planner = Planner(run_id=run_id, log_dir=_LOG_DIR)
    try:
        plan = planner.plan(prompt)  # loads cached plan
    except (RuntimeError, ValueError) as exc:
        return json.dumps({"error": f"Could not load plan: {exc}"})

    router = Router()
    executor = Executor(
        state=state,
        router=router,
        output_dir=_OUTPUT_DIR,
        max_workers=max_workers,
        retry_limit=retry_limit,
    )
    summary = executor.run(plan)
    summary["run_id"] = run_id
    return json.dumps(summary, indent=2)


def _format_dry_run(plan: dict) -> str:
    """Format a plan as readable text for dry-run responses."""
    lines = ["Task plan (dry run — nothing executed):\n"]
    for wave in plan["waves"]:
        lines.append(f"Wave {wave['wave']}:")
        for task in wave["tasks"]:
            deps = ", ".join(task.get("dependencies", [])) or "none"
            lines.append(f"  [{task['type']}] {task['id']}")
            lines.append(f"    Task: {task['task']}")
            lines.append(f"    Deps: {deps}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
