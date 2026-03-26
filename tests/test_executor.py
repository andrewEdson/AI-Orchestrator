"""
Integration tests for the Executor using mock agents.

All CLI calls are mocked via environment variables (CLAUDE_MOCK=1, COPILOT_MOCK=1)
so these tests run without any installed external tools.
"""

import os
import json
import tempfile
from pathlib import Path

import pytest

# Enable mock agents for the entire test session
os.environ["CLAUDE_MOCK"] = "1"
os.environ["COPILOT_MOCK"] = "1"


from orchestrator.executor import Executor
from orchestrator.router import Router
from orchestrator.utils.state import StateManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_plan(*waves_spec):
    """
    Build a minimal plan dict from specs like:
        [("task_a", "simple", []), ("task_b", "complex", ["task_a"])]
    Each spec is (id, type, dependencies).
    """
    waves = []
    for wave_num, tasks_spec in enumerate(waves_spec, start=1):
        waves.append({
            "wave": wave_num,
            "tasks": [
                {"id": tid, "task": f"Do {tid}", "type": ttype, "dependencies": deps}
                for tid, ttype, deps in tasks_spec
            ],
        })
    return {"waves": waves}


@pytest.fixture()
def tmp_dirs(tmp_path):
    outputs = tmp_path / "outputs"
    logs = tmp_path / "logs"
    outputs.mkdir()
    logs.mkdir()
    return str(outputs), str(logs)


@pytest.fixture()
def make_executor(tmp_dirs):
    outputs_dir, logs_dir = tmp_dirs

    def _factory(plan, run_id="test_run", max_workers=2, retry_limit=2):
        state = StateManager(run_id=run_id, log_dir=logs_dir)
        state.set_prompt("test prompt")
        state.set_plan(plan["waves"])
        router = Router(verbose=False)
        executor = Executor(
            state=state,
            router=router,
            output_dir=outputs_dir,
            max_workers=max_workers,
            retry_limit=retry_limit,
            dry_run=False,
            verbose=False,
        )
        return executor, state

    return _factory


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestExecutorHappyPath:
    def test_single_wave_all_complete(self, make_executor):
        plan = _make_plan([("setup", "simple", []), ("config", "simple", [])])
        executor, state = make_executor(plan)
        summary = executor.run(plan)
        assert summary["completed"] == 2
        assert summary["failed"] == 0

    def test_multi_wave_sequential(self, make_executor):
        plan = _make_plan(
            [("wave1_task", "simple", [])],
            [("wave2_task", "complex", ["wave1_task"])],
        )
        executor, state = make_executor(plan)
        summary = executor.run(plan)
        assert summary["completed"] == 2
        assert summary["failed"] == 0

    def test_output_files_created(self, make_executor, tmp_dirs):
        outputs_dir, _ = tmp_dirs
        plan = _make_plan([("mytask", "simple", [])])
        executor, state = make_executor(plan)
        executor.run(plan)
        assert (Path(outputs_dir) / "mytask.txt").exists()

    def test_complex_task_uses_claude(self, make_executor):
        plan = _make_plan([("arch_task", "complex", [])])
        executor, state = make_executor(plan)
        executor.run(plan)
        task_state = state.get_task("arch_task")
        assert task_state["agent"] == "claude"

    def test_simple_task_uses_copilot(self, make_executor):
        plan = _make_plan([("boilerplate", "simple", [])])
        executor, state = make_executor(plan)
        executor.run(plan)
        task_state = state.get_task("boilerplate")
        assert task_state["agent"] == "copilot"


class TestResumeSupport:
    def test_already_completed_tasks_are_skipped(self, make_executor):
        plan = _make_plan([("task_a", "simple", []), ("task_b", "simple", [])])
        executor, state = make_executor(plan, run_id="resume_test")

        # Simulate task_a already completed
        state._state["tasks"]["task_a"]["status"] = "completed"
        state._state["tasks"]["task_a"]["output_file"] = "/tmp/fake.txt"
        state.save()

        summary = executor.run(plan)
        # task_b should still complete; task_a was skipped
        assert summary["completed"] == 1

    def test_state_persisted_after_run(self, make_executor, tmp_dirs):
        _, logs_dir = tmp_dirs
        plan = _make_plan([("persist_task", "simple", [])])
        executor, state = make_executor(plan, run_id="persist_test")
        executor.run(plan)

        # Reload state from disk
        reloaded = StateManager(run_id="persist_test", log_dir=logs_dir)
        assert reloaded.is_completed("persist_task")


class TestDryRun:
    def test_dry_run_does_not_execute(self, tmp_dirs):
        outputs_dir, logs_dir = tmp_dirs
        plan = _make_plan([("dry_task", "simple", [])])
        state = StateManager(run_id="dry_run_test", log_dir=logs_dir)
        state.set_prompt("test")
        state.set_plan(plan["waves"])
        router = Router()
        executor = Executor(
            state=state,
            router=router,
            output_dir=outputs_dir,
            dry_run=True,
        )
        executor.run(plan)
        # No output file should be created in dry-run mode
        assert not (Path(outputs_dir) / "dry_task.txt").exists()
