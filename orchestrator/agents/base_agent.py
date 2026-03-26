"""
Abstract base class that every agent must implement.

Adding a new agent:
1. Subclass BaseAgent
2. Implement execute()
3. Register in router.py
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AgentResult:
    """
    Returned by every agent's execute() call.

    Attributes:
        success:    True if the task completed without error.
        output:     Raw stdout / response text from the agent.
        stderr:     Raw stderr, if any.
        exit_code:  Process exit code (0 = success).
        agent_name: Which agent produced this result.
        duration:   Wall-clock seconds the agent spent on the task.
        metadata:   Arbitrary extra data agents may attach.
    """

    success: bool
    output: str
    stderr: str = ""
    exit_code: int = 0
    agent_name: str = ""
    duration: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def short_summary(self, max_chars: int = 120) -> str:
        snippet = self.output.strip()[:max_chars]
        return f"[{self.agent_name}] exit={self.exit_code} | {snippet!r}"


class BaseAgent(ABC):
    """
    Contract that all execution agents must satisfy.

    Subclasses are expected to be stateless — each execute() call is
    independent and should not rely on previous calls.
    """

    # Human-readable name used in logs and state files
    name: str = "base"

    def __init__(self, verbose: bool = False, timeout: int = 300) -> None:
        self.verbose = verbose
        self.timeout = timeout  # seconds before a subprocess is killed

    @abstractmethod
    def execute(
        self,
        task: dict[str, Any],
        output_file: Optional[str] = None,
        dependency_context: Optional[dict[str, str]] = None,
    ) -> AgentResult:
        """
        Run the agent against a single task dict.

        Args:
            task:               The task dict from the planner (must contain 'id' and 'task').
            output_file:        If provided, write raw output to this path in addition to
                                returning it in AgentResult.
            dependency_context: Optional {task_id: summary} of upstream task outputs to
                                inject into the prompt so this agent doesn't re-explore.

        Returns:
            An AgentResult describing the outcome.
        """

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _timed_run(self, fn, *args, **kwargs) -> tuple[Any, float]:
        """Run a callable and return (result, elapsed_seconds)."""
        start = time.monotonic()
        result = fn(*args, **kwargs)
        return result, time.monotonic() - start

    def _write_output(self, output_file: Optional[str], content: str) -> None:
        """Persist agent output to a file if a path was given."""
        if not output_file:
            return
        from pathlib import Path

        path = Path(output_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
