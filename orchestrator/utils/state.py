"""
Persistent run state — written after every task transition so the orchestrator
can resume from the last known-good point after a crash or interruption.

State is stored as a single JSON file:  logs/{run_id}_state.json
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

# Valid task lifecycle statuses
TASK_STATUSES = {"pending", "running", "completed", "failed", "escalated", "skipped"}


class StateManager:
    """
    Manage lifecycle state for a single orchestration run.

    The backing file is written atomically (write-then-rename) so a crash
    mid-write never leaves a corrupt state file.
    """

    def __init__(self, run_id: str, log_dir: str = "logs") -> None:
        self.run_id = run_id
        self._path = Path(log_dir) / f"{run_id}_state.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "run_id": run_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "prompt": "",
            "tasks": {},   # task_id -> TaskState dict
            "waves": [],   # ordered list of wave summaries
            "summary": {},
        }

        # Load existing state if present (for --resume)
        if self._path.exists():
            self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        with open(self._path, "r", encoding="utf-8") as fh:
            self._state = json.load(fh)

    def save(self) -> None:
        """Write state atomically with a per-call unique tmp file (thread-safe)."""
        with self._lock:
            self._state["updated_at"] = time.time()
            # Unique tmp name avoids two threads clobbering each other's tmp file
            tmp = self._path.with_name(f"{self._path.stem}_{uuid.uuid4().hex[:8]}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._state, fh, indent=2)
            tmp.replace(self._path)

    # ------------------------------------------------------------------
    # Run-level
    # ------------------------------------------------------------------

    def set_prompt(self, prompt: str) -> None:
        self._state["prompt"] = prompt
        self.save()

    def set_plan(self, waves: list[dict[str, Any]]) -> None:
        """Record the planner's wave output and initialise task entries."""
        self._state["waves"] = waves
        for wave in waves:
            for task in wave.get("tasks", []):
                tid = task["id"]
                if tid not in self._state["tasks"]:
                    self._state["tasks"][tid] = {
                        "id": tid,
                        "task": task["task"],
                        "type": task.get("type", "simple"),
                        "dependencies": task.get("dependencies", []),
                        "wave": wave["wave"],
                        "status": "pending",
                        "agent": None,
                        "attempts": 0,
                        "started_at": None,
                        "finished_at": None,
                        "output_file": None,
                        "error": None,
                    }
        self.save()

    # ------------------------------------------------------------------
    # Task-level
    # ------------------------------------------------------------------

    def mark_running(self, task_id: str, agent: str) -> None:
        self._update_task(task_id, status="running", agent=agent, started_at=time.time())

    def mark_completed(self, task_id: str, output_file: str) -> None:
        self._update_task(
            task_id,
            status="completed",
            output_file=output_file,
            finished_at=time.time(),
        )

    def mark_failed(self, task_id: str, error: str) -> None:
        t = self._state["tasks"][task_id]
        self._update_task(
            task_id,
            status="failed",
            error=error,
            attempts=t["attempts"] + 1,
            finished_at=time.time(),
        )

    def mark_escalated(self, task_id: str) -> None:
        self._update_task(task_id, status="escalated")

    def increment_attempts(self, task_id: str) -> int:
        t = self._state["tasks"][task_id]
        new_count = t["attempts"] + 1
        self._update_task(task_id, attempts=new_count)
        return new_count

    def get_task(self, task_id: str) -> dict[str, Any]:
        return dict(self._state["tasks"][task_id])

    def get_task_status(self, task_id: str) -> str:
        return self._state["tasks"][task_id]["status"]

    def is_completed(self, task_id: str) -> bool:
        return self.get_task_status(task_id) == "completed"

    def all_tasks(self) -> list[dict[str, Any]]:
        return [dict(t) for t in self._state["tasks"].values()]

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------

    def set_summary(self, summary: dict[str, Any]) -> None:
        self._state["summary"] = summary
        self.save()

    def get_summary(self) -> dict[str, Any]:
        return dict(self._state["summary"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _update_task(self, task_id: str, **fields: Any) -> None:
        if task_id not in self._state["tasks"]:
            raise KeyError(f"Unknown task ID: {task_id}")
        # Update the in-memory dict, then flush. save() acquires its own lock.
        self._state["tasks"][task_id].update(fields)
        self.save()  # save() is internally thread-safe

    # ------------------------------------------------------------------
    # Convenience queries
    # ------------------------------------------------------------------

    def completed_ids(self) -> set[str]:
        return {
            tid for tid, t in self._state["tasks"].items() if t["status"] == "completed"
        }

    def failed_ids(self) -> set[str]:
        return {
            tid for tid, t in self._state["tasks"].items() if t["status"] == "failed"
        }

    @property
    def state_path(self) -> Path:
        return self._path
