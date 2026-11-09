"""Reads the latest health-check results file and derives per-area and
overall status for the home-page widget."""

import json
import os
import time

from api.core.config import HEALTH_STATUS_PATH

STALENESS_THRESHOLD_SECONDS = 3 * 60 * 60

AREA_GROUPS = {
    "Core Service": ["boot", "db", "restart", "invariants", "isolation"],
    "Federation Access": ["federation", "parity"],
    "Downloads": ["downloads", "concurrency", "kill", "long"],
}

_AREA_SUMMARIES = {
    "ok": {
        "Core Service": "App and database healthy",
        "Federation Access": "OSDF federation reachable",
        "Downloads": "Dataset transfers working",
    },
    "degraded": {
        "Core Service": "Some core services experiencing issues",
        "Federation Access": "Federation access intermittent",
        "Downloads": "Some transfers may be slow or failing",
    },
    "down": {
        "Core Service": "Core service errors detected",
        "Federation Access": "Federation unreachable",
        "Downloads": "Download failures detected",
    },
}


def _classify_area(results: list, test_areas: list[str]) -> str:
    passed = 0
    failed = 0
    for r in results:
        area = r.get("test", "").split("::")[0]
        area = area.removeprefix("zz_")
        if area in test_areas:
            if r["status"] == "PASS":
                passed += 1
            elif r["status"] == "FAIL":
                failed += 1
    if failed == 0:
        return "ok"
    total = passed + failed
    if total > 0 and failed / total < 0.5:
        return "degraded"
    return "down"


def get_health_status() -> dict:
    unknown_response = {
        "overall": "unknown",
        "last_checked_at": None,
        "summary": "Status unknown — health check hasn’t run recently",
        "areas": [],
    }

    if not os.path.exists(HEALTH_STATUS_PATH):
        return unknown_response

    try:
        with open(HEALTH_STATUS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return unknown_response

    env_unhealthy = data.get("env_unhealthy")
    aborted = data.get("aborted")

    run_timestamp = os.path.getmtime(HEALTH_STATUS_PATH)
    age_seconds = time.time() - run_timestamp
    if age_seconds > STALENESS_THRESHOLD_SECONDS:
        unknown_response["last_checked_at"] = run_timestamp
        return unknown_response

    if env_unhealthy or aborted:
        unknown_response["last_checked_at"] = run_timestamp
        return unknown_response

    results = data.get("results", [])
    areas = []
    worst = "ok"
    for group_name, test_areas in AREA_GROUPS.items():
        status = _classify_area(results, test_areas)
        areas.append({
            "name": group_name,
            "status": status,
            "summary": _AREA_SUMMARIES[status][group_name],
        })
        if status == "down":
            worst = "down"
        elif status == "degraded" and worst != "down":
            worst = "degraded"

    overall_summaries = {
        "ok": "All systems operating normally",
        "degraded": "Some services experiencing issues",
        "down": "Service disruptions detected",
    }

    return {
        "overall": worst,
        "last_checked_at": run_timestamp,
        "summary": overall_summaries[worst],
        "areas": areas,
    }
