#!/bin/bash
# RECOMMENDED if real cron access is available on the submission host.
#
# Crontab entry (every 90 minutes, offset from the hour):
#   0 */3 * * *   /path/to/pelicanvil/scripts/health_check_cron.sh
#   45 1,4,7,10,13,16,19,22 * * *  /path/to/pelicanvil/scripts/health_check_cron.sh
#
# The two-line crontab above approximates 90-minute spacing. A simpler
# alternative that runs every 2 hours instead:
#   0 */2 * * *   /path/to/pelicanvil/scripts/health_check_cron.sh
#
# This script simply submits a standard-tier test run via the existing
# sbatch entry point. The test harness itself writes results.json; the
# pelicanvil_tests.sbatch script copies it to the shared health_status.json
# location after a standard run completes (see the addition at the bottom
# of that file).

set -euo pipefail

SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
APP_DIR="$( cd -- "$SCRIPT_DIR/.." &> /dev/null && pwd )"

sbatch "$APP_DIR/tests/pelicanvil_tests.sbatch" standard
