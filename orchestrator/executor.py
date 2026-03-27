"""
Execution engine — runs task waves in order, tasks within each wave in parallel.

Flow per wave:
    1. Filter out tasks already completed (resume support).
    2. Submit tasks to a ThreadPoolExecutor (up to max_workers).
    3. On failure, retry up to retry_limit times.
    4. If a simple/copilot task still fails after retries → escalate to Claude.
    5. After all tasks in the wave finish → run the Validator.
    6. Move on to the next wave.
"""

from __future__ import annotations

import re
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

from orchestrator.agents.base_agent import AgentResult
from orchestrator.router import Router
from orchestrator.utils.logger import get_logger
from orchestrator.utils.state import StateManager
from orchestrator.utils.task_graph import TaskGraph
from orchestrator.validator import Validator

log = get_logger("orchestrator.executor")
console = Console(stderr=True)


def _extract_output_summary(output: str, max_words: int = 250) -> str:
    """
    Extract a compact summary from raw agent output.

    Prefers the '## Summary' section (ClaudeAgent always writes one).
    Falls back to extracting fenced code-block filenames.
    Final fallback: first max_words words of the raw output.
    """
    match = re.search(r"##\s*Summary\s*\n([\s\S]+?)(?=\n##|\Z)", output, re.IGNORECASE)
    if match:
        words = match.group(1).strip().split()
        return " ".join(words[:max_words])

    headers = re.findall(r"```\w+:([^\n]+)", output)
    if headers:
        text = "Files written: " + ", ".join(headers)
        words = text.split()
        return " ".join(words[:max_words])

    words = output.strip().split()
    return " ".join(words[:max_words])


class Executor:
    """
    Drives the full wave-by-wave execution lifecycle.

    Args:
        state:       Run state manager (handles persistence).
        router:      Maps task types to agent instances.
        output_dir:  Directory where per-task output files are written.
        max_workers: Maximum parallel tasks within a single wave.
        retry_limit: How many times to retry a failing task before escalation.
        dry_run:     If True, log what would happen but never call agents.
        verbose:     Pass verbose flag through to agents.
    """

    def __init__(
        self,
        state: StateManager,
        router: Router,
        output_dir: str = "outputs",
        max_workers: int = 4,
        retry_limit: int = 3,
        dry_run: bool = False,
        verbose: bool = False,
        context_doc: str = "",
    ) -> None:
        self.state = state
        self.router = router
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max_workers
        self.retry_limit = retry_limit
        self.dry_run = dry_run
        self.verbose = verbose
        self.context_doc = context_doc
        self.validator = Validator(state=state, verbose=verbose)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, plan: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the full plan and return a run summary dict.

        Args:
            plan: Validated plan dict from the Planner.

        Returns:
            Summary dict with counts and per-task status.
        """
        waves = plan["waves"]
        log.info("Starting execution: %d wave(s), max_workers=%d", len(waves), self.max_workers)

        total_completed = 0
        total_failed = 0

        for wave_entry in waves:
            wave_num = wave_entry["wave"]
            tasks = wave_entry["tasks"]

            # Filter tasks already completed from a previous (partial) run
            pending = [t for t in tasks if not self.state.is_completed(t["id"])]
            skipped = len(tasks) - len(pending)

            if skipped:
                log.info("Wave %d: skipping %d already-completed task(s)", wave_num, skipped)

            if not pending:
                log.info("Wave %d: all tasks already complete — skipping wave", wave_num)
                continue

            log.info("Wave %d: executing %d task(s)…", wave_num, len(pending))
            self._display_wave_header(wave_num, pending)

            if self.dry_run:
                self._dry_run_wave(wave_num, pending)
                continue

            completed, failed = self._execute_wave(wave_num, pending)
            total_completed += completed
            total_failed += failed

            # Validate after each wave
            validation_ok = self.validator.validate_wave(wave_num, pending)
            if not validation_ok:
                log.warning("Wave %d validation reported issues — see logs for details", wave_num)

        summary = self._build_summary(total_completed, total_failed)
        self.state.set_summary(summary)
        return summary

    # ------------------------------------------------------------------
    # Wave execution
    # ------------------------------------------------------------------

    def _execute_wave(
        self, wave_num: int, tasks: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Run all tasks in a wave in parallel. Returns (completed, failed) counts."""
        completed = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            future_to_task: dict[Future, dict[str, Any]] = {
                pool.submit(self._execute_task_with_retry, task): task
                for task in tasks
            }

            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    result: AgentResult = future.result()
                    if result.success:
                        completed += 1
                        log.info(
                            "Task '%s' completed by %s (%.1fs)",
                            task["id"], result.agent_name, result.duration,
                        )
                    else:
                        failed += 1
                        log.error("Task '%s' FAILED: %s", task["id"], result.stderr[:200])
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    log.exception("Unhandled exception in task '%s': %s", task["id"], exc)
                    self.state.mark_failed(task["id"], str(exc))

        return completed, failed

    # ------------------------------------------------------------------
    # Single-task execution with retry + escalation
    # ------------------------------------------------------------------

    def _execute_task_with_retry(self, task: dict[str, Any]) -> AgentResult:
        """
        Execute a single task, retrying on failure up to retry_limit times.
        If the task is 'simple' and retries are exhausted, escalate to Claude.
        """
        task_id = task["id"]
        task_type = task.get("type", "simple")
        output_file = str(self.output_dir / f"{task_id}.txt")

        # Inject project context doc into task (reuses existing 'context' key)
        enriched_task = dict(task)
        if self.context_doc:
            existing_ctx = task.get("context") or ""
            enriched_task["context"] = (
                f"[Project Context]\n{self.context_doc}\n\n{existing_ctx}".strip()
            )

        # Resolve summaries from upstream dependencies
        dep_ids = task.get("dependencies", [])
        dependency_context = self.state.get_dependency_summaries(dep_ids) if dep_ids else None

        agent = self.router.get_agent(task_type)
        self.state.mark_running(task_id, agent.name)

        last_result: Optional[AgentResult] = None

        for attempt in range(1, self.retry_limit + 1):
            log.debug("Task '%s': attempt %d/%d via %s", task_id, attempt, self.retry_limit, agent.name)
            try:
                result = agent.execute(
                    enriched_task,
                    output_file=output_file,
                    dependency_context=dependency_context,
                )
            except Exception as exc:  # noqa: BLE001
                result = AgentResult(
                    success=False,
                    output="",
                    stderr=str(exc),
                    exit_code=1,
                    agent_name=agent.name,
                )

            self.state.increment_attempts(task_id)
            last_result = result

            if result.success:
                self.state.mark_completed(task_id, output_file)
                self.state.set_task_summary(task_id, _extract_output_summary(result.output))
                return result

            log.warning(
                "Task '%s' attempt %d failed (exit=%d): %s",
                task_id, attempt, result.exit_code, result.stderr[:120],
            )
            time.sleep(min(2 ** (attempt - 1), 8))  # exponential back-off, cap 8s

        # ----------------------------------------------------------------
        # Escalation: only for non-Claude tasks
        # ----------------------------------------------------------------
        if task_type != "complex":
            log.warning(
                "Task '%s' exhausted retries on %s — escalating to Claude",
                task_id, agent.name,
            )
            self.state.mark_escalated(task_id)
            escalation_agent = self.router.get_escalation_agent()
            escalation_output_file = str(self.output_dir / f"{task_id}_escalated.txt")
            try:
                result = escalation_agent.execute(
                    enriched_task,
                    output_file=escalation_output_file,
                    dependency_context=dependency_context,
                )
            except Exception as exc:  # noqa: BLE001
                result = AgentResult(
                    success=False,
                    output="",
                    stderr=str(exc),
                    exit_code=1,
                    agent_name=escalation_agent.name,
                )

            if result.success:
                self.state.mark_completed(task_id, escalation_output_file)
                self.state.set_task_summary(task_id, _extract_output_summary(result.output))
                log.info("Task '%s' completed via escalation to Claude", task_id)
                return result
            else:
                log.error("Task '%s' failed even after escalation", task_id)
                self.state.mark_failed(task_id, result.stderr)
                return result

        # Claude task failed after retries
        self.state.mark_failed(task_id, last_result.stderr if last_result else "unknown error")
        return last_result or AgentResult(
            success=False, output="", stderr="no result", exit_code=1, agent_name="none"
        )

    # ------------------------------------------------------------------
    # Dry-run mode
    # ------------------------------------------------------------------

    def _dry_run_wave(self, wave_num: int, tasks: list[dict[str, Any]]) -> None:
        for task in tasks:
            agent_name = self.router.get_agent(task.get("type", "simple")).name
            console.print(
                f"  [dim]DRY-RUN[/dim] wave={wave_num} "
                f"id=[bold]{task['id']}[/bold] "
                f"type={task['type']} "
                f"agent={agent_name} "
                f"deps={task.get('dependencies', [])}"
            )

    # ------------------------------------------------------------------
    # Display helpers
    # ------------------------------------------------------------------

    def _display_wave_header(self, wave_num: int, tasks: list[dict[str, Any]]) -> None:
        table = Table(
            title=f"Wave {wave_num}",
            show_header=True,
            header_style="bold cyan",
            border_style="dim",
        )
        table.add_column("ID", style="bold")
        table.add_column("Type", justify="center")
        table.add_column("Agent", justify="center")
        table.add_column("Dependencies")
        table.add_column("Task", no_wrap=False, max_width=60)

        for task in tasks:
            agent_name = self.router.get_agent(task.get("type", "simple")).name
            deps = ", ".join(task.get("dependencies", [])) or "—"
            table.add_row(
                task["id"],
                task.get("type", "simple"),
                agent_name,
                deps,
                task["task"],
            )

        console.print(table)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def _build_summary(self, completed: int, failed: int) -> dict[str, Any]:
        all_tasks = self.state.all_tasks()
        return {
            "total": len(all_tasks),
            "completed": completed,
            "failed": failed,
            "tasks": {
                t["id"]: {
                    "status": t["status"],
                    "agent": t["agent"],
                    "attempts": t["attempts"],
                    "output_file": t["output_file"],
                    "duration": (
                        round(t["finished_at"] - t["started_at"], 2)
                        if t.get("finished_at") and t.get("started_at")
                        else None
                    ),
                }
                for t in all_tasks
            },
        }
