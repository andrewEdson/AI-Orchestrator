"""
GitHub Copilot CLI agent.

Wraps `gh copilot suggest` for lightweight, "simple" code generation tasks.

CLI contract assumed:
    gh copilot suggest -t code "<prompt>"
    → writes suggestion to stdout, exits 0 on success

Environment variable overrides:
    GH_CLI_PATH      — path to the gh binary (default: "gh")
    COPILOT_MOCK     — if set to "1", return a canned mock response

Notes on `gh copilot suggest` target types:
    -t shell   → shell one-liners / scripts
    -t code    → general code (default used here)
    -t explain → explanations (not used for generation)
"""

from __future__ import annotations

import os
import subprocess
import time
from typing import Any, Optional

from orchestrator.agents.base_agent import AgentResult, BaseAgent

# Prefix injected before every task prompt so Copilot produces file-level output
_TASK_PREFIX = (
    "You are an expert software engineer. "
    "Produce complete, production-quality code. "
    "No placeholders, no TODO stubs. "
    "Wrap output in a fenced code block with the filename as the language tag. "
    "Task: "
)


class CopilotAgent(BaseAgent):
    """Executes simple tasks via the GitHub Copilot CLI (`gh copilot suggest`)."""

    name = "copilot"

    def __init__(self, verbose: bool = False, timeout: int = 120) -> None:
        super().__init__(verbose=verbose, timeout=timeout)
        self.cli_path = os.environ.get("GH_CLI_PATH", "gh")
        self._mock = os.environ.get("COPILOT_MOCK", "0") == "1"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def execute(self, task: dict[str, Any], output_file: Optional[str] = None) -> AgentResult:
        """
        Send the task to Copilot CLI and return the result.
        """
        prompt = _TASK_PREFIX + task["task"]

        if self._mock:
            return self._mock_response(task, output_file)

        start = time.monotonic()
        try:
            result = self._run_subprocess(prompt)
        except FileNotFoundError:
            return AgentResult(
                success=False,
                output="",
                stderr=(
                    f"GitHub CLI not found at '{self.cli_path}'. "
                    "Install with: brew install gh  &&  gh auth login  &&  "
                    "gh extension install github/gh-copilot"
                ),
                exit_code=127,
                agent_name=self.name,
                duration=time.monotonic() - start,
            )

        duration = time.monotonic() - start
        success = result.returncode == 0
        output = result.stdout or ""
        stderr = result.stderr or ""

        # gh copilot sometimes writes "Suggestion:" header — strip it
        output = _strip_copilot_header(output)

        self._write_output(output_file, output)

        if self.verbose:
            print(f"[copilot] task={task['id']} exit={result.returncode} dur={duration:.1f}s")

        return AgentResult(
            success=success,
            output=output,
            stderr=stderr,
            exit_code=result.returncode,
            agent_name=self.name,
            duration=duration,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_subprocess(self, prompt: str) -> subprocess.CompletedProcess:
        cmd = [self.cli_path, "copilot", "suggest", "-t", "code", prompt]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

    def _mock_response(self, task: dict[str, Any], output_file: Optional[str]) -> AgentResult:
        output = (
            f"```python:{task['id']}.py\n"
            f"# Mock Copilot output for task: {task['task']}\n"
            f"def {task['id']}():\n"
            f"    pass\n"
            "```\n"
        )
        self._write_output(output_file, output)
        return AgentResult(
            success=True,
            output=output,
            agent_name=self.name,
            duration=0.05,
            metadata={"mock": True},
        )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _strip_copilot_header(text: str) -> str:
    """Remove the 'Suggestion:' prefix that gh copilot sometimes emits."""
    lines = text.splitlines()
    trimmed = []
    skip = True
    for line in lines:
        if skip and line.strip().lower().startswith("suggestion:"):
            skip = False
            continue
        skip = False
        trimmed.append(line)
    return "\n".join(trimmed) if trimmed else text
