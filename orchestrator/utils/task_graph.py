"""
Task dependency graph — validates plans, detects cycles, and groups tasks
into execution waves.

The planner already produces wave-grouped output, but this module provides
an independent validation layer and can recompute waves from raw task lists
if needed (e.g. for resumption logic).
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


class TaskGraph:
    """
    Directed acyclic graph over task IDs.

    Usage
    -----
    graph = TaskGraph()
    graph.add_tasks(tasks)       # list of dicts with 'id' and 'dependencies'
    graph.validate()             # raises ValueError on cycles
    waves = graph.compute_waves()  # list of lists, ordered by execution wave
    """

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._deps: dict[str, list[str]] = defaultdict(list)  # id -> [dep_ids]
        self._rdeps: dict[str, list[str]] = defaultdict(list)  # id -> [dependent_ids]

    # ------------------------------------------------------------------
    # Building the graph
    # ------------------------------------------------------------------

    def add_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Register a list of task dicts (each must have 'id' and 'dependencies')."""
        for task in tasks:
            task_id = task["id"]
            self._tasks[task_id] = task
            for dep in task.get("dependencies", []):
                self._deps[task_id].append(dep)
                self._rdeps[dep].append(task_id)

    def add_task(self, task: dict[str, Any]) -> None:
        self.add_tasks([task])

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self) -> None:
        """
        Verify the graph is acyclic and all dependency IDs exist.

        Raises:
            ValueError: if a cycle or unknown dependency is found.
        """
        # Check all referenced dependencies actually exist
        for task_id, deps in self._deps.items():
            for dep in deps:
                if dep not in self._tasks:
                    raise ValueError(
                        f"Task '{task_id}' depends on unknown task '{dep}'"
                    )

        # Kahn's algorithm to detect cycles
        in_degree = {tid: len(deps) for tid, deps in self._deps.items()}
        for tid in self._tasks:
            if tid not in in_degree:
                in_degree[tid] = 0

        queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
        visited = 0

        while queue:
            tid = queue.popleft()
            visited += 1
            for dependent in self._rdeps.get(tid, []):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if visited != len(self._tasks):
            raise ValueError(
                "Cycle detected in task dependency graph — cannot determine execution order."
            )

    # ------------------------------------------------------------------
    # Wave computation
    # ------------------------------------------------------------------

    def compute_waves(self) -> list[list[dict[str, Any]]]:
        """
        Return tasks grouped into execution waves via topological sort.

        All tasks in wave N can run in parallel; no task in wave N depends
        on any other task in the same wave.
        """
        if not self._tasks:
            return []

        in_degree = {tid: 0 for tid in self._tasks}
        for tid, deps in self._deps.items():
            in_degree[tid] = len(deps)

        waves: list[list[dict[str, Any]]] = []
        remaining = set(self._tasks.keys())

        while remaining:
            # Tasks whose dependencies are all satisfied
            wave_ids = [tid for tid in remaining if in_degree[tid] == 0]
            if not wave_ids:
                raise ValueError("Unexpected cycle — could not compute execution waves.")

            waves.append([self._tasks[tid] for tid in sorted(wave_ids)])

            for tid in wave_ids:
                remaining.remove(tid)
                for dependent in self._rdeps.get(tid, []):
                    in_degree[dependent] -= 1

        return waves

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._tasks[task_id]

    def all_task_ids(self) -> list[str]:
        return list(self._tasks.keys())

    def dependency_ids(self, task_id: str) -> list[str]:
        return list(self._deps.get(task_id, []))

    def __len__(self) -> int:
        return len(self._tasks)
