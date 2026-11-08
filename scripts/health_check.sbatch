#!/bin/bash
# FALLBACK: Slurm self-resubmitting health check — use this if real cron
# access is NOT available on the submission host.
#
# Modeled on scripts/indexing_worker.sbatch's self-resubmission pattern:
# the resubmission happens from THIS host-side script (not from inside a
# container), since the container has no `sbatch` binary (2026-08-24
# lesson from the indexing worker).
#
# Submit once to start the self-renewing chain:
#   sbatch scripts/health_check.sbatch
#
# Each cycle:
#   1. Submits a standard-tier test run and waits for it to finish.
#   2. Resubmits itself with a --begin delay (~90 minutes from now).
#
# If cron access becomes available, switch to health_check_cron.sh
# instead — it's simpler and doesn't consume a Slurm job slot while
# sleeping.
#
#SBATCH --job-name=pelicanvil-health-check
#SBATCH --account=cda090008
#SBATCH --partition=shared
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=1
#SBATCH --mem=512M
#SBATCH --time=02:00:00
#SBATCH --output=/anvil/projects/x-cda090008/anvil_reu26_pj4/pelican-ui-shared/health_check_%j.log

set -euo pipefail

APP_DIR="$HOME/ondemand/dev/pelicanvil"
RESUBMIT_DELAY_MINUTES=90

cd "$APP_DIR"

# Submit the actual test run and capture its job ID so we can wait on it.
TEST_JOB_ID=$(sbatch --parsable "$APP_DIR/tests/pelicanvil_tests.sbatch" standard)
echo "Submitted standard test run as job $TEST_JOB_ID; waiting for it to finish."

# Poll until the test job finishes (squeue returns empty when it's done).
while squeue -j "$TEST_JOB_ID" -h 2>/dev/null | grep -q "$TEST_JOB_ID"; do
    sleep 60
done
echo "Test job $TEST_JOB_ID finished."

# Resubmit ourselves for the next cycle.
echo "Resubmitting health check in ${RESUBMIT_DELAY_MINUTES} minutes."
sbatch --begin="now+${RESUBMIT_DELAY_MINUTES}minutes" "$APP_DIR/scripts/health_check.sbatch"
