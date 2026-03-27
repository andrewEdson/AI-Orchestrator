"""
Planner — decomposes a user prompt into a structured wave plan via Claude.

The planner calls the Claude CLI, expects JSON back, validates the schema,
and returns a normalised plan dict.  It also optionally writes the raw plan
to disk so the executor can skip re-planning on --resume runs.
"""

from __future__ import annotations

import json
import re
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any, Optional

from orchestrator.utils.logger import get_logger

log = get_logger("orchestrator.planner")

# ---------------------------------------------------------------------------
# System prompt given to Claude to produce the task plan
# ---------------------------------------------------------------------------
_PLANNER_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a senior software architect acting as an orchestration planner.

    Your job is to decompose a user's high-level request into a set of
    parallelisable execution waves for a multi-agent code generation system.

    RULES:
    1. Return ONLY valid JSON — no markdown fences, no commentary, nothing else.
    2. Tasks within the same wave have NO inter-dependencies and can run in parallel.
    3. Tasks in wave N+1 may depend on tasks from earlier waves.
    4. Assign type "simple" to tasks that are straightforward (single files,
       boilerplate, configs, small utilities).
    5. Assign type "medium" to tasks that span multiple files but follow
       well-understood patterns (CRUD routes, model classes, test suites).
    6. Assign type "complex" to tasks involving architecture decisions,
       security-sensitive code, novel algorithms, or integration of 3+ dependencies.
    7. Use snake_case task IDs (e.g. "setup_database_schema").
    8. Keep each task atomic and independently executable — an agent must be
       able to complete it from the description alone.
    9. Aim for 3-8 tasks per wave; more granular is better.
    10. Include a top-level "context_doc" field: a compact 50-100 word project
        context document (plain bullet points only, no headers) that every
        code-writing agent will receive. Cover: project name/description, primary
        language/framework, key shared module names or interfaces, file layout
        conventions, and any shared constants or config keys.

    SCHEMA (strictly follow this):
    {
      "context_doc": "• Project: ...\n• Language: ...\n• ...",
      "waves": [
        {
          "wave": 1,
          "tasks": [
            {
              "id": "snake_case_unique_id",
              "task": "Specific, actionable task description",
              "type": "simple" | "medium" | "complex",
              "dependencies": []
            }
          ]
        },
        {
          "wave": 2,
          "tasks": [
            {
              "id": "another_task_id",
              "task": "Depends on wave-1 output, so placed here",
              "type": "medium",
              "dependencies": ["snake_case_unique_id"]
            }
          ]
        }
      ]
    }
""")


class Planner:
    """
    Uses Claude to decompose a prompt into execution waves.

    A cached plan file is checked first so re-runs (or --resume) do not
    trigger redundant LLM calls.
    """

    def __init__(
        self,
        run_id: str,
        log_dir: str = "logs",
        claude_cli: str = "claude",
        model: Optional[str] = None,
        verbose: bool = False,
        timeout: int = 120,
        mock: bool = False,
    ) -> None:
        self.run_id = run_id
        self._plan_path = Path(log_dir) / f"{run_id}_plan.json"
        self._plan_path.parent.mkdir(parents=True, exist_ok=True)
        self.claude_cli = claude_cli
        self.model = model
        self.verbose = verbose
        self.timeout = timeout
        self.mock = mock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def plan(self, prompt: str) -> dict[str, Any]:
        """
        Return a validated plan dict for the given prompt.

        If a cached plan exists for this run_id it is returned directly
        (idempotent — safe to call multiple times).
        """
        if self._plan_path.exists():
            log.info("Loading cached plan from %s", self._plan_path)
            with open(self._plan_path, encoding="utf-8") as fh:
                return json.load(fh)

        log.info("Generating plan via Claude…")
        raw = self._call_claude(prompt)
        plan = self._parse_and_validate(raw)
        if "context_doc" not in plan:
            plan["context_doc"] = ""
        self._save_plan(plan)
        return plan

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _call_claude(self, prompt: str) -> str:
        if self.mock:
            return self._mock_plan(prompt)

        full_prompt = f"{_PLANNER_SYSTEM_PROMPT}\n\nUser request: {prompt}"
        cmd = [self.claude_cli, "--print", "--dangerously-skip-permissions", full_prompt]
        if self.model:
            cmd += ["--model", self.model]

        if self.verbose:
            log.debug("Planner command: %s", " ".join(cmd[:2]) + " <prompt>")

        start = time.monotonic()
        try:
            result = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Claude CLI not found at '{self.claude_cli}'. "
                "Install Claude Code: https://claude.ai/code"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Claude planner timed out after {self.timeout}s"
            ) from exc

        duration = time.monotonic() - start
        log.debug("Claude planner responded in %.1fs", duration)

        if result.returncode != 0:
            raise RuntimeError(
                f"Claude planner exited with code {result.returncode}.\n"
                f"stderr: {result.stderr.strip()}"
            )

        return result.stdout

    def _parse_and_validate(self, raw: str) -> dict[str, Any]:
        """Extract JSON from Claude's response and validate the schema."""
        # Claude sometimes wraps JSON in a code fence — strip it
        text = _extract_json(raw)

        try:
            plan = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Planner returned invalid JSON.\n"
                f"Parse error: {exc}\n"
                f"Raw output:\n{raw[:800]}"
            ) from exc

        _validate_plan_schema(plan)
        return plan

    def _save_plan(self, plan: dict[str, Any]) -> None:
        with open(self._plan_path, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
        log.debug("Plan saved to %s", self._plan_path)

    # ------------------------------------------------------------------
    # Mock helper (used when CLAUDE_MOCK=1 or --dry-run tests)
    # ------------------------------------------------------------------

    def _mock_plan(self, prompt: str) -> str:
        """Return a minimal two-wave plan for testing without live CLI calls."""
        plan = {
            "context_doc": f"• Project: {prompt[:80]}\n• Language: Python",
            "waves": [
                {
                    "wave": 1,
                    "tasks": [
                        {
                            "id": "setup_project_structure",
                            "task": f"Create the project directory structure and pyproject.toml for: {prompt}",
                            "type": "simple",
                            "dependencies": [],
                        },
                        {
                            "id": "write_database_schema",
                            "task": "Write the initial database schema and migration files.",
                            "type": "complex",
                            "dependencies": [],
                        },
                    ],
                },
                {
                    "wave": 2,
                    "tasks": [
                        {
                            "id": "implement_auth_module",
                            "task": "Implement JWT authentication (register, login, token refresh).",
                            "type": "complex",
                            "dependencies": ["setup_project_structure", "write_database_schema"],
                        },
                        {
                            "id": "write_api_routes",
                            "task": "Write REST API route handlers using the project structure.",
                            "type": "simple",
                            "dependencies": ["setup_project_structure"],
                        },
                    ],
                },
            ]
        }
        return json.dumps(plan)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> str:
    """
    Strip markdown code fences if present, then return the raw JSON string.
    Handles ```json ... ``` and ``` ... ``` wrappers.
    """
    fence_pattern = re.compile(r"```(?:json)?\s*([\s\S]+?)\s*```", re.IGNORECASE)
    match = fence_pattern.search(text)
    if match:
        return match.group(1)
    return text.strip()


def _validate_plan_schema(plan: dict[str, Any]) -> None:
    """Raise ValueError with a descriptive message if the plan is malformed."""
    if not isinstance(plan, dict):
        raise ValueError("Plan must be a JSON object.")
    if "waves" not in plan:
        raise ValueError("Plan missing required key 'waves'.")
    if not isinstance(plan["waves"], list) or not plan["waves"]:
        raise ValueError("'waves' must be a non-empty list.")

    seen_ids: set[str] = set()
    for wave in plan["waves"]:
        if "wave" not in wave or "tasks" not in wave:
            raise ValueError(f"Wave entry missing 'wave' or 'tasks': {wave}")
        for task in wave["tasks"]:
            for required in ("id", "task", "type"):
                if required not in task:
                    raise ValueError(f"Task missing required key '{required}': {task}")
            if task["type"] not in ("simple", "medium", "complex"):
                raise ValueError(
                    f"Task '{task['id']}' has invalid type '{task['type']}'. "
                    "Expected 'simple', 'medium', or 'complex'."
                )
            if task["id"] in seen_ids:
                raise ValueError(f"Duplicate task ID: '{task['id']}'")
            seen_ids.add(task["id"])
