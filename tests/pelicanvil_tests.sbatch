#!/bin/bash
# PELICANVIL test suite: ONE entry point, selectable depth.
#
#   sbatch tests/pelicanvil_tests.sbatch quick       # ~5 min    (smoke: boots, DB basics, 404, auth, 1 download)
#   sbatch tests/pelicanvil_tests.sbatch standard    # ~30 min   (adds directories, +/space, restart, concurrency, kill -9, CLI parity)
#   sbatch tests/pelicanvil_tests.sbatch deep        # hours     (standard x {uvicorn, wsgi} + many-file, long job, soak, repeated kills)
#   sbatch tests/pelicanvil_tests.sbatch selftest    # ~1 min    (harness self-check, needs NO federation: run this first)
#   bash   tests/pelicanvil_tests.sbatch setup       # one-time, on a login node: build the test Python venv
#
# The --time limit set in the directives below is static; shorten a run with e.g.
#   sbatch --time=00:20:00 tests/pelicanvil_tests.sbatch quick
#   sbatch --time=01:30:00 tests/pelicanvil_tests.sbatch standard
#
# Optional environment overrides (export them, or `ONLY=... sbatch ...`):
#   ONLY=<pytest -k expr>   run a subset            REPS=<n>          repetitions for repeated deep tests (default 3)
#   DURATION=<seconds>      cap for the long job (default 3600)     SOAK_DURATION=<seconds>  soak length (default 1800)
#   SERVER=uvicorn|wsgi|uvicorn,wsgi   server mode(s) (default: uvicorn; deep: both)      LONG_MODES=<modes> for the long job
#   APP_DIR=<checkout>      app under test (default: this checkout) -- e.g. a `git worktree` of 4766e6d for before/after
#   PV_PYTHON=<python>      use this interpreter      TEST_VENV=<dir>   test venv (default /anvil/scratch/$USER/pelicanvil-tests/venv)
#   PELICAN_BIN=<pelican>   pelican CLI for the parity tests      PV_MODULES="python/3.11"  modules to load first
#   KEEP_DATA=1             keep downloaded data even on a fully passing run
#   STALL_SECONDS / LONG_STALL_SECONDS / LONG_MAX_GB  stall thresholds and scratch cap for the long job
#
# Results: logs/test-runs/<jobid>-<depth>/results.log  (+ results.json, junit.xml, failures/, anomalies/, server-logs/)
# Exit code: 0 pass | 1 test failures | 2 harness error | 3 ENVIRONMENT UNHEALTHY (no tests ran) | 4 aborted (walltime/scancel)
#
# Account/partition are copied from scripts/indexing_worker.sbatch. I/O-bound, so 4 cores is plenty.
#SBATCH --job-name=pelicanvil-tests
#SBATCH --account=cda090008
#SBATCH --partition=shared
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=08:00:00
#SBATCH --signal=B:USR1@180
#SBATCH --output=pelicanvil-tests-%j.out

set -uo pipefail

DEPTH="${1:-${DEPTH:-standard}}"
case "$DEPTH" in quick|standard|deep|selftest|setup) ;; *)
    echo "usage: sbatch tests/pelicanvil_tests.sbatch [quick|standard|deep|selftest]   (or: bash tests/pelicanvil_tests.sbatch setup)" >&2
    exit 2 ;;
esac

# SUITE_DIR: the checkout the tests live in (where you ran sbatch from). Slurm runs a COPY of this script
# from its spool dir, so $0 is not reliable -- the same reason scripts/indexing_worker.sbatch pins APP_DIR.
SUITE_DIR="${SUITE_DIR:-${SLURM_SUBMIT_DIR:-$PWD}}"
if [ ! -f "$SUITE_DIR/tests/run_suite.py" ]; then
    if [ -f "$HOME/ondemand/dev/pelicanvil/tests/run_suite.py" ]; then SUITE_DIR="$HOME/ondemand/dev/pelicanvil"
    else echo "cannot find tests/run_suite.py under $SUITE_DIR; cd to your checkout before sbatch, or set SUITE_DIR" >&2; exit 2; fi
fi
export APP_DIR="${APP_DIR:-$SUITE_DIR}"
SCRATCH_BASE="${PV_SCRATCH_BASE:-/anvil/scratch/${USER:-$(id -un)}/pelicanvil-tests}"
export PV_SCRATCH_BASE="$SCRATCH_BASE"
TEST_VENV="${TEST_VENV:-$SCRATCH_BASE/venv}"

if [ -n "${PV_MODULES:-}" ]; then
    # shellcheck disable=SC1091
    source /etc/profile 2>/dev/null; module load $PV_MODULES || echo "warning: module load $PV_MODULES failed" >&2
fi

py_ok() { "$1" - <<'EOF' >/dev/null 2>&1
import sys
assert sys.version_info >= (3, 11)
import pytest, yaml, requests, pelicanfs, fastapi, uvicorn, a2wsgi, aiowebdav2
EOF
}

setup_venv() {
    echo "== building test venv at $TEST_VENV =="
    mkdir -p "$(dirname "$TEST_VENV")"
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.11 "$TEST_VENV" || return 1
        ( cd "$APP_DIR" && uv export --frozen --no-dev --no-hashes --no-emit-project -o "$TEST_VENV/lock-requirements.txt" ) \
            || { echo "uv export failed"; return 1; }
        uv pip install --python "$TEST_VENV/bin/python" -r "$TEST_VENV/lock-requirements.txt" -r "$SUITE_DIR/tests/requirements.txt" || return 1
    else
        local py="${PV_PYTHON_BOOTSTRAP:-python3}"
        "$py" -c 'import sys; assert sys.version_info >= (3,11), sys.version' || { echo "need Python >= 3.11 (set PV_MODULES or PV_PYTHON_BOOTSTRAP)"; return 1; }
        "$py" -m venv "$TEST_VENV" || return 1
        "$TEST_VENV/bin/pip" install --upgrade pip
        "$TEST_VENV/bin/pip" install -r "$SUITE_DIR/tests/requirements.txt" "a2wsgi>=1.10.10" fastapi "fsspec>=2026.6.0" jinja2 "pelicanfs>=1.3.1" "uvicorn>=0.16.0" || return 1
    fi
}

if [ "$DEPTH" = "setup" ]; then
    setup_venv && py_ok "$TEST_VENV/bin/python" && { echo "OK: $TEST_VENV is ready"; exit 0; }
    echo "setup FAILED" >&2; exit 2
fi

# Choose the interpreter: PV_PYTHON, else the test venv (built on first use), else fail loudly.
if [ -n "${PV_PYTHON:-}" ]; then PY="$PV_PYTHON"
else
    PY="$TEST_VENV/bin/python"
    if [ ! -x "$PY" ] || ! py_ok "$PY"; then
        echo "test venv missing or incomplete; trying to build it (needs network on this node; otherwise run 'bash tests/pelicanvil_tests.sbatch setup' on a login node)"
        setup_venv || { echo "cannot build the test venv; see message above" >&2; exit 2; }
    fi
fi
py_ok "$PY" || { echo "$PY is missing required packages (pytest, pyyaml, requests, pelicanfs, fastapi, uvicorn, a2wsgi)" >&2; exit 2; }
if [ "$DEPTH" = "deep" ] && ! "$PY" -c 'import waitress' >/dev/null 2>&1; then
    echo "warning: waitress is not installed: SERVER=wsgi tests will be SKIPPED (and reported as such)" >&2
fi

export PV_RUN_ID="${SLURM_JOB_ID:-local$(date +%Y%m%d-%H%M%S)}"
export PV_LONG_MODES="${LONG_MODES:-}"
[ -z "$PV_LONG_MODES" ] && unset PV_LONG_MODES
export DEPTH
echo "== pelicanvil tests: depth=$DEPTH job=$PV_RUN_ID suite=$SUITE_DIR app=$APP_DIR python=$PY =="

cd "$SUITE_DIR" || exit 2
"$PY" -u "$SUITE_DIR/tests/run_suite.py" "$DEPTH" &
PY_PID=$!
# Forward Slurm's pre-walltime USR1 / TERM to the runner so it writes a summary before dying.
trap 'kill -USR1 "$PY_PID" 2>/dev/null' USR1
trap 'kill -TERM "$PY_PID" 2>/dev/null' TERM INT
wait "$PY_PID"; rc=$?
while kill -0 "$PY_PID" 2>/dev/null; do wait "$PY_PID"; rc=$?; done

RUN_DIR="$SUITE_DIR/logs/test-runs/$PV_RUN_ID-$DEPTH"
[ -n "${PV_LOG_ROOT:-}" ] && RUN_DIR="$PV_LOG_ROOT/$PV_RUN_ID-$DEPTH"
if [ -d "$RUN_DIR" ] && [ -f "$RUN_DIR/results.log" ]; then
    echo "== summary =="; sed -n '/^SUMMARY/,/^elapsed/p' "$RUN_DIR/results.log"
    echo "== full results: $RUN_DIR/results.log (exit code $rc) =="
    # keep Slurm's own stdout next to the results too (best effort; it is still being written)
    [ -n "${SLURM_JOB_ID:-}" ] && cp "$SUITE_DIR/pelicanvil-tests-$SLURM_JOB_ID.out" "$RUN_DIR/slurm.out" 2>/dev/null
fi
exit "$rc"
