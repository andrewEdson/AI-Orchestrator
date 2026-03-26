"""
Task router — maps task types to agent classes.

Design goals:
- Zero business logic; pure dispatch.
- Easy to extend: add a new entry to AGENT_REGISTRY.
- The executor imports only this module; it never imports agents directly.
"""

from __future__ import annotations

import os
from typing import Optional, Type

from orchestrator.agents.base_agent import BaseAgent
from orchestrator.agents.claude_agent import ClaudeAgent
from orchestrator.agents.copilot_agent import CopilotAgent


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
# Maps task['type'] → agent class.
# "medium" is handled specially in Router.get_agent() — it uses ClaudeAgent
# with a cheaper model (haiku by default, override via CLAUDE_MEDIUM_MODEL).
# ---------------------------------------------------------------------------
AGENT_REGISTRY: dict[str, Type[BaseAgent]] = {
    "simple": CopilotAgent,
    "complex": ClaudeAgent,
    # Future agents can be added here, e.g.:
    # "shell": ShellAgent,
    # "test": TestAgent,
}

# The escalation agent is always Claude (used when a lower-tier agent fails).
ESCALATION_AGENT: Type[BaseAgent] = ClaudeAgent


class Router:
    """
    Instantiate and return the correct agent for a given task.

    The router owns agent construction so callers do not need to know
    which class is used.
    """

    def __init__(self, verbose: bool = False, timeout: int = 300) -> None:
        self.verbose = verbose
        self.timeout = timeout
        # Cache agent instances to avoid redundant construction
        self._cache: dict[str, BaseAgent] = {}

    def get_agent(self, task_type: str) -> BaseAgent:
        """
        Return the agent for the given task type.

        'medium' maps to ClaudeAgent with a cheaper model (haiku).
        Falls back to ClaudeAgent if the type is not recognised.
        """
        normalised = task_type.lower().strip()
        if normalised == "medium":
            return self._get_or_create_medium()
        agent_cls = AGENT_REGISTRY.get(normalised, ClaudeAgent)
        return self._get_or_create(agent_cls)

    def _get_or_create_medium(self) -> BaseAgent:
        """Return a ClaudeAgent configured with the medium (haiku) model."""
        cache_key = "claude-medium"
        if cache_key not in self._cache:
            model = os.environ.get("CLAUDE_MEDIUM_MODEL", "claude-haiku-4-5")
            agent = ClaudeAgent(verbose=self.verbose, timeout=self.timeout, model=model)
            agent.name = "claude-haiku"
            self._cache[cache_key] = agent
        return self._cache[cache_key]

    def get_escalation_agent(self) -> BaseAgent:
        """Return the escalation agent (always Claude)."""
        return self._get_or_create(ESCALATION_AGENT)

    def register(self, task_type: str, agent_cls: Type[BaseAgent]) -> None:
        """
        Dynamically register a new agent at runtime.

        Useful for plugins or test overrides:
            router.register("mytype", MyCustomAgent)
        """
        AGENT_REGISTRY[task_type.lower()] = agent_cls
        # Invalidate cache so the new class is used on next request
        self._cache.pop(task_type.lower(), None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_or_create(self, agent_cls: Type[BaseAgent]) -> BaseAgent:
        key = agent_cls.name
        if key not in self._cache:
            self._cache[key] = agent_cls(verbose=self.verbose, timeout=self.timeout)
        return self._cache[key]
