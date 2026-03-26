# Orchestrator

A production-quality CLI tool that manages multi-agent code generation workflows. It uses Claude as the planner/orchestrator and routes tasks to GitHub Copilot CLI (simple tasks) or Claude CLI (complex tasks), running them in parallel waves.

---

## How it works

```
User prompt
    │
    ▼
┌─────────────┐
│   Planner   │  Claude decomposes the prompt into parallel waves
└──────┬──────┘
       │  structured JSON plan
       ▼
┌─────────────┐
│   Router    │  simple → CopilotAgent  │  complex → ClaudeAgent
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────┐
│              Executor                   │
│  Wave 1: [task_a ‖ task_b ‖ task_c]    │  parallel (ThreadPoolExecutor)
│  Wave 2: [task_d ‖ task_e]             │  waits for wave 1
│  Wave 3: [task_f]                      │  waits for wave 2
└──────┬──────────────────────────────────┘
       │
       ▼
┌─────────────┐
│  Validator  │  lint · Claude review (optional)
└──────┬──────┘
       │
       ▼
  Run Summary  (console + logs/RUN_ID_state.json)
```

**Retry + Escalation:** if a Copilot task fails after N retries, it is automatically re-submitted to Claude. If Claude itself fails, the task is marked failed.

---

## Prerequisites

| Tool | Required | Notes |
|------|----------|-------|
| Python 3.11+ | Yes | |
| Claude Code CLI | Yes (for live runs) | `npm install -g @anthropic-ai/claude-code` |
| GitHub CLI + Copilot extension | Yes (for live runs) | `brew install gh && gh extension install github/gh-copilot` |

---

## Quick start

```bash
# 1. Clone / navigate to the project
cd "AI Orchestrator"

# 2. Create venv + install deps + run (all-in-one)
./run_task.sh "build a fullstack app with JWT auth"

# Or manually:
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
orchestrator "build a fullstack app with JWT auth"
```

---

## CLI reference

```
orchestrator [OPTIONS] PROMPT
```

| Option | Default | Description |
|--------|---------|-------------|
| `--max-workers` / `-w` | `4` | Max parallel agent tasks per wave |
| `--retry-limit` / `-r` | `3` | Retries before escalation to Claude |
| `--dry-run` / `-d` | off | Print the plan without executing |
| `--verbose` / `-v` | off | Debug-level console logging |
| `--resume RUN_ID` | — | Continue an interrupted run |
| `--output-dir` | `./outputs` | Where task output files are written |
| `--log-dir` | `./logs` | Where log and state files are stored |
| `--mock` | off | Use mock agents (no live CLI calls) |
| `--claude-review` | off | Ask Claude to review outputs after each wave |
| `--summary-file PATH` | — | Write final summary JSON to a file |

### Sub-commands

```bash
# List previous runs
orchestrator list-runs

# Show the saved plan for a run
orchestrator show-plan RUN_ID
```

---

## Examples

```bash
# Dry-run — see the task plan without executing
orchestrator --dry-run "build a REST API with Postgres"

# High-parallelism run with verbose logging
orchestrator --max-workers 8 --verbose "build a data pipeline with Airflow"

# Test without live CLI tools installed
orchestrator --mock "build a fullstack app"

# Resume after a crash
orchestrator --resume run_1711234567_abc123 "build a fullstack app"

# Write summary JSON for CI integration
orchestrator --summary-file results.json "build a CLI tool"
```

---

## Using a pre-built plan

Skip the planner step by pointing the executor at `tasks_example.json`:

```python
import json
from orchestrator.executor import Executor
from orchestrator.router import Router
from orchestrator.utils.state import StateManager

plan = json.loads(open("tasks_example.json").read())
state = StateManager("my_run")
state.set_prompt("fullstack app")
state.set_plan(plan["waves"])

executor = Executor(state=state, router=Router())
summary = executor.run(plan)
```

---

## File layout

```
orchestrator/
├── cli.py              CLI entry point (Typer)
├── planner.py          Decomposes prompts → wave plan via Claude
├── executor.py         Wave-by-wave parallel execution engine
├── router.py           Maps task types to agent classes
├── validator.py        Post-wave lint + optional Claude review
└── agents/
    ├── base_agent.py   Abstract contract (AgentResult, BaseAgent)
    ├── claude_agent.py Wraps `claude --print`
    └── copilot_agent.py Wraps `gh copilot suggest`
    utils/
    ├── logger.py       Structured logging (Rich + file)
    ├── task_graph.py   Dependency validation + wave computation
    └── state.py        Persistent run state (JSON, atomic writes)

tests/
├── test_task_graph.py
├── test_planner.py
├── test_executor.py
└── test_router.py

outputs/            Per-task output files  (task_id.txt)
logs/               Per-run log + state files
tasks_example.json  Example 4-wave plan (fullstack auth app)
run_task.sh         Bootstrap helper script
```

---

## Adding a new agent

1. Subclass `BaseAgent` in `orchestrator/agents/my_agent.py`
2. Implement `execute(task, output_file) -> AgentResult`
3. Register in `orchestrator/router.py`:

```python
from orchestrator.agents.my_agent import MyAgent

AGENT_REGISTRY = {
    "simple": CopilotAgent,
    "complex": ClaudeAgent,
    "my_type": MyAgent,   # ← add this
}
```

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `CLAUDE_CLI_PATH` | `claude` | Path to Claude Code binary |
| `CLAUDE_MODEL` | (CLI default) | Override model for Claude calls |
| `CLAUDE_MOCK` | `0` | Set `1` to use mock Claude (no CLI needed) |
| `GH_CLI_PATH` | `gh` | Path to GitHub CLI binary |
| `COPILOT_MOCK` | `0` | Set `1` to use mock Copilot (no CLI needed) |

---

## Running tests

```bash
pip install -e ".[dev]"
pytest
pytest --cov=orchestrator --cov-report=term-missing
```

All tests run in mock mode — no live CLI tools required.

---

## Output format

Task outputs are written to `outputs/{task_id}.txt` (or `outputs/{task_id}_escalated.txt` for escalated tasks). Agents are instructed to wrap code in fenced blocks:

````
```python:src/auth.py
# full implementation here
```

## Summary
- Implemented JWT login/register
- Used bcrypt for password hashing
````

---

## Resuming interrupted runs

Every state transition is written atomically to `logs/{run_id}_state.json`. To resume:

```bash
orchestrator --resume run_1711234567_abc123 "original prompt"
```

The executor will skip any tasks whose status is already `completed`.
