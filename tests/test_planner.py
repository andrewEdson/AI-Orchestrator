"""Tests for the Planner module (mock mode only — no live Claude calls)."""

import json
import tempfile
from pathlib import Path

import pytest

from orchestrator.planner import Planner, _extract_json, _validate_plan_schema


# ---------------------------------------------------------------------------
# Unit tests for helper functions
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_bare_json_passthrough(self):
        raw = '{"waves": []}'
        assert _extract_json(raw) == '{"waves": []}'

    def test_strips_json_fence(self):
        raw = "```json\n{\"waves\": []}\n```"
        assert _extract_json(raw).strip() == '{"waves": []}'

    def test_strips_plain_fence(self):
        raw = "```\n{\"waves\": []}\n```"
        assert _extract_json(raw).strip() == '{"waves": []}'

    def test_strips_whitespace(self):
        raw = "   {\"waves\": []}   "
        assert _extract_json(raw) == '{"waves": []}'


class TestValidatePlanSchema:
    def _minimal_plan(self):
        return {
            "waves": [
                {
                    "wave": 1,
                    "tasks": [
                        {"id": "task_a", "task": "Do A", "type": "simple", "dependencies": []}
                    ],
                }
            ]
        }

    def test_valid_plan_passes(self):
        _validate_plan_schema(self._minimal_plan())  # no exception

    def test_missing_waves_key_raises(self):
        with pytest.raises(ValueError, match="waves"):
            _validate_plan_schema({})

    def test_empty_waves_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_plan_schema({"waves": []})

    def test_missing_task_id_raises(self):
        plan = self._minimal_plan()
        del plan["waves"][0]["tasks"][0]["id"]
        with pytest.raises(ValueError, match="'id'"):
            _validate_plan_schema(plan)

    def test_invalid_type_raises(self):
        plan = self._minimal_plan()
        plan["waves"][0]["tasks"][0]["type"] = "ultra"
        with pytest.raises(ValueError, match="invalid type"):
            _validate_plan_schema(plan)

    def test_duplicate_id_raises(self):
        plan = self._minimal_plan()
        plan["waves"][0]["tasks"].append(
            {"id": "task_a", "task": "Duplicate", "type": "simple", "dependencies": []}
        )
        with pytest.raises(ValueError, match="Duplicate task ID"):
            _validate_plan_schema(plan)


# ---------------------------------------------------------------------------
# Integration test using mock Planner
# ---------------------------------------------------------------------------

class TestPlannerMock:
    def test_mock_plan_returns_valid_structure(self, tmp_path):
        planner = Planner(
            run_id="test_run",
            log_dir=str(tmp_path),
            mock=True,
        )
        plan = planner.plan("build a REST API")
        assert "waves" in plan
        assert len(plan["waves"]) >= 1
        for wave in plan["waves"]:
            assert "wave" in wave
            assert "tasks" in wave
            for task in wave["tasks"]:
                assert "id" in task
                assert "task" in task
                assert task["type"] in ("simple", "complex")

    def test_plan_is_cached_on_second_call(self, tmp_path):
        planner = Planner(run_id="cache_test", log_dir=str(tmp_path), mock=True)
        plan1 = planner.plan("anything")
        # Sabotage the mock so a second call would produce different output
        planner._mock = lambda p: '{"waves": [{"wave": 1, "tasks": []}]}'
        plan2 = planner.plan("anything")
        # Should return the cached version
        assert plan1 == plan2

    def test_plan_file_written(self, tmp_path):
        planner = Planner(run_id="write_test", log_dir=str(tmp_path), mock=True)
        planner.plan("build something")
        plan_file = tmp_path / "write_test_plan.json"
        assert plan_file.exists()
        data = json.loads(plan_file.read_text())
        assert "waves" in data
