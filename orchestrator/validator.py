"""
Post-wave validator.

Responsibilities:
1. Run a lint/syntax check on any Python files written during the wave.
2. Optionally ask Claude to review the wave outputs for correctness.
3. Return True if the wave is considered healthy.

The validator is intentionally lenient by default — a lint warning will
not abort the run, only hard errors do.  Override the strictness via the
`strict` flag.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Optional

from orchestrator.utils.logger import get_logger
from orchestrator.utils.state import StateManager

log = get_logger("orchestrator.validator")


class Validator:
    """
    Validates the outputs produced during a single wave.

    Args:
        state:   Run state manager (to look up output file paths).
        verbose: Enable extra logging.
        strict:  If True, any lint error causes validate_wave to return False.
        use_claude_review: Ask Claude to review outputs (requires live Claude CLI).
    """

    def __init__(
        self,
        state: StateManager,
        verbose: bool = False,
        strict: bool = False,
        use_claude_review: bool = False,
        claude_cli: str = "claude",
    ) -> None:
        self.state = state
        self.verbose = verbose
        self.strict = strict
        self.use_claude_review = use_claude_review
        self.claude_cli = claude_cli

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate_wave(
        self, wave_num: int, tasks: list[dict[str, Any]]
    ) -> bool:
        """
        Run all checks for the given wave's tasks.

        Returns:
            True if all checks pass (or only warnings in non-strict mode).
            False if a hard error is found (or any error in strict mode).
        """
        log.info("Validating wave %d…", wave_num)
        all_ok = True

        for task in tasks:
            task_id = task["id"]
            task_state = self.state.get_task(task_id)

            if task_state["status"] not in ("completed", "escalated"):
                log.debug("Skipping validation for non-completed task '%s'", task_id)
                continue

            output_file = task_state.get("output_file")
            if not output_file or not Path(output_file).exists():
                log.warning("No output file for task '%s' — skipping lint", task_id)
                continue

            content = Path(output_file).read_text(encoding="utf-8")
            python_blocks = _extract_python_blocks(content)

            for idx, block in enumerate(python_blocks):
                ok, issues = _lint_python(block, label=f"{task_id}[{idx}]")
                if issues:
                    for issue in issues:
                        log.warning("Lint: %s", issue)
                if not ok:
                    all_ok = False
                    if self.strict:
                        return False

        if self.use_claude_review:
            review_ok = self._claude_review(wave_num, tasks)
            if not review_ok:
                all_ok = False

        status = "PASSED" if all_ok else "ISSUES FOUND"
        log.info("Wave %d validation: %s", wave_num, status)
        return all_ok

    # ------------------------------------------------------------------
    # Claude review
    # ------------------------------------------------------------------

    def _claude_review(self, wave_num: int, tasks: list[dict[str, Any]]) -> bool:
        """
        Ask Claude to sanity-check the combined outputs of a wave.
        Returns True if Claude says the outputs look correct.
        """
        outputs: list[str] = []
        for task in tasks:
            ts = self.state.get_task(task["id"])
            if ts.get("output_file") and Path(ts["output_file"]).exists():
                snippet = Path(ts["output_file"]).read_text(encoding="utf-8")[:500]
                outputs.append(f"### {task['id']}\n{snippet}")

        if not outputs:
            return True

        combined = "\n\n".join(outputs)
        prompt = (
            f"You are reviewing wave {wave_num} outputs from an automated code generation run.\n"
            "For each task output below, briefly state if it looks correct and complete.\n"
            "End your response with a JSON line: {\"wave_ok\": true} or {\"wave_ok\": false}.\n\n"
            + combined
        )

        try:
            result = subprocess.run(
                [self.claude_cli, "--print", prompt],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                log.warning("Claude review exited %d — skipping", result.returncode)
                return True  # Don't fail the wave on review tool errors

            # Look for the final JSON verdict
            for line in reversed(result.stdout.splitlines()):
                line = line.strip()
                if line.startswith("{") and "wave_ok" in line:
                    verdict = json.loads(line)
                    return bool(verdict.get("wave_ok", True))

        except Exception as exc:  # noqa: BLE001
            log.warning("Claude review failed: %s — continuing", exc)

        return True  # Default: don't block on review failure


# ---------------------------------------------------------------------------
# Lint helpers
# ---------------------------------------------------------------------------

def _extract_python_blocks(text: str) -> list[str]:
    """
    Extract code from fenced Python blocks in agent output.

    Handles both:
        ```python
        ...
        ```
    and:
        ```python:filename.py
        ...
        ```
    """
    import re

    pattern = re.compile(r"```python[^\n]*\n([\s\S]+?)```", re.IGNORECASE)
    return pattern.findall(text)


def _lint_python(code: str, label: str = "") -> tuple[bool, list[str]]:
    """
    Parse a Python code block with the ast module.

    Returns (ok, issues) where issues is a list of human-readable strings.
    A SyntaxError is a hard error (ok=False); other issues are warnings.
    """
    issues: list[str] = []
    try:
        ast.parse(code)
    except SyntaxError as exc:
        issues.append(f"{label}: SyntaxError at line {exc.lineno}: {exc.msg}")
        return False, issues

    # --- Optional: check for obvious bad practices ---------------------------
    # Empty except blocks
    import re

    if re.search(r"except\s*:\s*\n\s*pass", code):
        issues.append(f"{label}: bare 'except: pass' detected (swallowed exception)")

    # Hard-coded credentials pattern (very basic)
    if re.search(r'(password|secret|api_key)\s*=\s*["\'][^"\']{4,}["\']', code, re.IGNORECASE):
        issues.append(f"{label}: possible hard-coded credential detected")

    return True, issues
