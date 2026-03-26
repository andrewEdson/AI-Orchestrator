#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# run_task.sh — helper script for running the orchestrator in common modes.
#
# Usage:
#   ./run_task.sh "build a fullstack app with auth"
#   ./run_task.sh --dry-run "build a REST API"
#   ./run_task.sh --mock "test without live CLI calls"
#   ./run_task.sh --resume RUN_ID "continue from a previous run"
# ---------------------------------------------------------------------------

set -euo pipefail

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RESET='\033[0m'

info()    { echo -e "${CYAN}[info]${RESET} $*"; }
success() { echo -e "${GREEN}[ok]${RESET}   $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET} $*"; }
error()   { echo -e "${RED}[error]${RESET} $*" >&2; }

# ---------------------------------------------------------------------------
# Check Python version
# ---------------------------------------------------------------------------
REQUIRED_PYTHON="3.11"
PYTHON_BIN="python3"

if ! command -v "$PYTHON_BIN" &>/dev/null; then
    error "python3 not found. Install Python ${REQUIRED_PYTHON}+."
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"; then
    :
else
    error "Python ${REQUIRED_PYTHON}+ required (found ${PYTHON_VERSION})."
    exit 1
fi

# ---------------------------------------------------------------------------
# Ensure virtual environment
# ---------------------------------------------------------------------------
VENV_DIR=".venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at ${VENV_DIR}…"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

# Activate venv
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"

# ---------------------------------------------------------------------------
# Install dependencies (idempotent)
# ---------------------------------------------------------------------------
if [[ ! -f "${VENV_DIR}/.deps_installed" ]]; then
    info "Installing dependencies from requirements.txt…"
    pip install --quiet --upgrade pip
    pip install --quiet -r requirements.txt
    touch "${VENV_DIR}/.deps_installed"
    success "Dependencies installed."
else
    info "Dependencies already installed. (Delete ${VENV_DIR}/.deps_installed to re-install.)"
fi

# ---------------------------------------------------------------------------
# Install the orchestrator package in editable mode
# ---------------------------------------------------------------------------
if ! python -c "import orchestrator" 2>/dev/null; then
    info "Installing orchestrator package…"
    pip install --quiet -e .
    success "Package installed."
fi

# ---------------------------------------------------------------------------
# Create output directories
# ---------------------------------------------------------------------------
mkdir -p outputs logs

# ---------------------------------------------------------------------------
# Parse arguments and delegate to orchestrator CLI
# ---------------------------------------------------------------------------
info "Launching orchestrator…"
echo ""

# All arguments are passed directly to the orchestrator CLI.
# Examples:
#   ./run_task.sh "build a REST API"
#   ./run_task.sh --dry-run "build a REST API"
#   ./run_task.sh --mock --verbose "build a REST API"
orchestrator "$@"
