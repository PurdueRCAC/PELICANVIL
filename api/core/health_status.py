"""Reads the latest health-check results file and derives a simple,
general-audience status for the home-page widget."""

import json
import os
import time

from api.core.config import HEALTH_STATUS_PATH

# If the last run is older than this many seconds, treat it as stale.
# ~3 hours — roughly 2x the 90-minute scheduled interval.
STALENESS_THRESHOLD_SECONDS = 3 * 60 * 60

_SUMMARY_OK = "All systems operating normally"
_SUMMARY_DEGRADED = "Some dataset access may be slower or unavailable"
_SUMMARY_DOWN = "Service disruptions detected"
_SUMMARY_UNKNOWN = "Status unknown — health check hasn’t run recently"


def get_health_status() -> dict:
    if not os.path.exists(HEALTH_STATUS_PATH):
        return {
            "overall": "unknown",
            "last_checked_at": None,
            "summary": _SUMMARY_UNKNOWN,
        }

    try:
        with open(HEALTH_STATUS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {
            "overall": "unknown",
            "last_checked_at": None,
            "summary": _SUMMARY_UNKNOWN,
        }

    elapsed_seconds = data.get("elapsed_seconds", 0)
    counts = data.get("counts", {})
    env_unhealthy = data.get("env_unhealthy")
    aborted = data.get("aborted")

    run_timestamp = os.path.getmtime(HEALTH_STATUS_PATH)
    age_seconds = time.time() - run_timestamp
    if age_seconds > STALENESS_THRESHOLD_SECONDS:
        return {
            "overall": "unknown",
            "last_checked_at": run_timestamp,
            "summary": _SUMMARY_UNKNOWN,
        }

    passed = counts.get("pass", 0)
    failed = counts.get("fail", 0)
    skipped = counts.get("skip", 0)
    total = passed + failed + skipped

    if env_unhealthy or aborted:
        overall = "unknown"
        summary = _SUMMARY_UNKNOWN
    elif failed == 0:
        overall = "ok"
        summary = _SUMMARY_OK
    elif total > 0 and failed / total < 0.5:
        overall = "degraded"
        summary = _SUMMARY_DEGRADED
    else:
        overall = "down"
        summary = _SUMMARY_DOWN

    return {
        "overall": overall,
        "last_checked_at": run_timestamp,
        "summary": summary,
    }
