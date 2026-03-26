"""Tests for the TaskGraph dependency validator."""

import pytest
from orchestrator.utils.task_graph import TaskGraph


def make_task(tid, deps=None, task_type="simple"):
    return {"id": tid, "task": f"Do {tid}", "type": task_type, "dependencies": deps or []}


class TestTaskGraphValidation:
    def test_linear_chain_is_valid(self):
        graph = TaskGraph()
        graph.add_tasks([make_task("a"), make_task("b", ["a"]), make_task("c", ["b"])])
        graph.validate()  # should not raise

    def test_parallel_tasks_no_deps(self):
        graph = TaskGraph()
        graph.add_tasks([make_task("x"), make_task("y"), make_task("z")])
        graph.validate()

    def test_unknown_dependency_raises(self):
        graph = TaskGraph()
        graph.add_tasks([make_task("a", deps=["nonexistent"])])
        with pytest.raises(ValueError, match="unknown task"):
            graph.validate()

    def test_direct_cycle_raises(self):
        graph = TaskGraph()
        # a depends on b, b depends on a
        graph.add_tasks([make_task("a", ["b"]), make_task("b", ["a"])])
        with pytest.raises(ValueError, match="[Cc]ycle"):
            graph.validate()

    def test_indirect_cycle_raises(self):
        graph = TaskGraph()
        graph.add_tasks([
            make_task("a", ["c"]),
            make_task("b", ["a"]),
            make_task("c", ["b"]),
        ])
        with pytest.raises(ValueError, match="[Cc]ycle"):
            graph.validate()

    def test_empty_graph_is_valid(self):
        graph = TaskGraph()
        graph.validate()  # no tasks — still valid


class TestWaveComputation:
    def test_no_deps_all_in_wave_1(self):
        graph = TaskGraph()
        graph.add_tasks([make_task("a"), make_task("b"), make_task("c")])
        waves = graph.compute_waves()
        assert len(waves) == 1
        ids = {t["id"] for t in waves[0]}
        assert ids == {"a", "b", "c"}

    def test_linear_chain_produces_one_task_per_wave(self):
        graph = TaskGraph()
        graph.add_tasks([
            make_task("step1"),
            make_task("step2", ["step1"]),
            make_task("step3", ["step2"]),
        ])
        waves = graph.compute_waves()
        assert len(waves) == 3
        assert waves[0][0]["id"] == "step1"
        assert waves[1][0]["id"] == "step2"
        assert waves[2][0]["id"] == "step3"

    def test_diamond_dependency(self):
        # a -> b, a -> c, b -> d, c -> d
        graph = TaskGraph()
        graph.add_tasks([
            make_task("a"),
            make_task("b", ["a"]),
            make_task("c", ["a"]),
            make_task("d", ["b", "c"]),
        ])
        waves = graph.compute_waves()
        wave_ids = [{t["id"] for t in w} for w in waves]
        assert {"a"} in wave_ids
        assert {"b", "c"} in wave_ids
        assert {"d"} in wave_ids

    def test_all_task_ids_present(self):
        graph = TaskGraph()
        tasks = [make_task(str(i), [str(i - 1)] if i > 0 else []) for i in range(5)]
        graph.add_tasks(tasks)
        waves = graph.compute_waves()
        all_ids = {t["id"] for wave in waves for t in wave}
        assert all_ids == {str(i) for i in range(5)}
