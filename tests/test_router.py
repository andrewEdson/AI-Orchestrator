"""Tests for the task router."""

import pytest

from orchestrator.agents.claude_agent import ClaudeAgent
from orchestrator.agents.copilot_agent import CopilotAgent
from orchestrator.router import Router


class TestRouter:
    def setup_method(self):
        self.router = Router()

    def test_simple_maps_to_copilot(self):
        agent = self.router.get_agent("simple")
        assert isinstance(agent, CopilotAgent)

    def test_complex_maps_to_claude(self):
        agent = self.router.get_agent("complex")
        assert isinstance(agent, ClaudeAgent)

    def test_unknown_type_falls_back_to_claude(self):
        agent = self.router.get_agent("nonexistent_type")
        assert isinstance(agent, ClaudeAgent)

    def test_case_insensitive(self):
        assert isinstance(self.router.get_agent("SIMPLE"), CopilotAgent)
        assert isinstance(self.router.get_agent("Complex"), ClaudeAgent)

    def test_escalation_agent_is_claude(self):
        agent = self.router.get_escalation_agent()
        assert isinstance(agent, ClaudeAgent)

    def test_agents_are_cached(self):
        a1 = self.router.get_agent("simple")
        a2 = self.router.get_agent("simple")
        assert a1 is a2  # same instance

    def test_custom_agent_registration(self):
        from orchestrator.agents.base_agent import AgentResult, BaseAgent

        class FakeAgent(BaseAgent):
            name = "fake"

            def execute(self, task, output_file=None):
                return AgentResult(success=True, output="ok", agent_name=self.name)

        self.router.register("custom", FakeAgent)
        agent = self.router.get_agent("custom")
        assert isinstance(agent, FakeAgent)
