"""
Claude Code CLI agent.

Wraps `claude --print "<prompt>"` for non-interactive, single-shot code
generation.  Claude is used for complex tasks and as the escalation target
when CopilotAgent retries are exhausted.

CLI contract assumed:
    claude --print "<prompt>"
    → writes output to stdout, exits 0 on success

Environment variable overrides (useful for testing):
    CLAUDE_CLI_PATH   — path to the claude binary (default: "claude")
    CLAUDE_MODEL      — model flag value (default: not passed, uses CLI default)
    CLAUDE_MOCK       — if set to "1", return a canned mock response
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import time
from typing import Any, Optional

from orchestrator.agents.base_agent import AgentResult, BaseAgent


# System prompt injected before every task to enforce structured, actionable output
_SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert software engineer executing a specific, well-defined task
    as part of a larger automated workflow.

    Rules:
    - Produce complete, runnable code — no placeholders or TODO stubs.
    - If creating a file, wrap the content in a fenced code block with the
      filename as the language tag, e.g. ```python:src/auth.py
    - After the code block(s), add a short "## Summary" section (≤5 bullet points).
    - Do NOT ask clarifying questions — work with the information given.
    - Do NOT include any preamble; start directly with the output.
""")


class ClaudeAgent(BaseAgent):
    """Executes tasks via the Claude Code CLI (`claude --print`)."""

    name = "claude"

    def __init__(
        self,
        verbose: bool = False,
        timeout: int = 300,
        model: Optional[str] = None,
    ) -> None:
        super().__init__(verbose=verbose, timeout=timeout)
        self.cli_path = os.environ.get("CLAUDE_CLI_PATH", "claude")
        self.model = model or os.environ.get("CLAUDE_MODEL")
        self._mock = os.environ.get("CLAUDE_MOCK", "0") == "1"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def execute(self, task: dict[str, Any], output_file: Optional[str] = None) -> AgentResult:
        """
        Send the task to Claude CLI and return the result.

        The full prompt is:
            <system instructions>
            ---
            Task ID: <id>
            Task:    <description>
        """
        prompt = self._build_prompt(task)

        if self._mock:
            return self._mock_response(task, output_file)

        start = time.monotonic()
        try:
            result = self._run_subprocess(prompt)
        except FileNotFoundError:
            return AgentResult(
                success=False,
                output="",
                stderr=f"Claude CLI not found at '{self.cli_path}'. "
                       "Install Claude Code: https://claude.ai/code",
                exit_code=127,
                agent_name=self.name,
                duration=time.monotonic() - start,
            )

        duration = time.monotonic() - start
        success = result.returncode == 0
        output = result.stdout or ""
        stderr = result.stderr or ""

        self._write_output(output_file, output)

        if self.verbose:
            print(f"[claude] task={task['id']} exit={result.returncode} dur={duration:.1f}s")

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

    def _build_prompt(self, task: dict[str, Any]) -> str:
        lines = [
            _SYSTEM_PROMPT,
            "---",
            f"Task ID: {task['id']}",
            f"Task:    {task['task']}",
        ]
        if task.get("context"):
            lines += ["", f"Context: {task['context']}"]
        return "\n".join(lines)

    def _run_subprocess(self, prompt: str) -> subprocess.CompletedProcess:
        cmd = [self.cli_path, "--print", prompt]
        if self.model:
            cmd += ["--model", self.model]

        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
        )

    def _mock_response(self, task: dict[str, Any], output_file: Optional[str]) -> AgentResult:
        output = (
            f"```python:{task['id']}.py\n"
            f"# Mock Claude output for task: {task['task']}\n"
            f"print('Hello from {task['id']}')\n"
            "```\n\n"
            "## Summary\n"
            f"- Implemented {task['id']} as requested.\n"
        )
        self._write_output(output_file, output)
        return AgentResult(
            success=True,
            output=output,
            agent_name=self.name,
            duration=0.1,
            metadata={"mock": True},
        )
