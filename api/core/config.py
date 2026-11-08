import os

DB_PATH = "/anvil/projects/x-cda090008/anvil_reu26_pj4/pelican-ui-shared/pelican.db"
#DB_PATH = "/anvil/scratch/x-bturner/pelican.db"

# Per-user local downloads history — deliberately separate from DB_PATH above.
# DB_PATH is a single shared, multi-user, admin-managed database; this one is
# single-user/local to whichever account is running this deployment, so it
# lives under that user's home directory instead of the shared project path.
DOWNLOADS_DB_PATH = os.path.join(os.path.expanduser("~"), ".pelican-ui", "downloads_history.db")

# Shared, catalog-wide indexing queue/history for the admin-triggered
# dataset-size Slurm worker (scripts/indexing_worker.py). Deliberately next
# to DB_PATH, not any user's $HOME like DOWNLOADS_DB_PATH above — every PUN
# process needs to see and append to the same queue, and so does the single
# shared worker, unlike per-user download history or per-user auth tokens.
INDEXING_QUEUE_PATH = os.path.join(os.path.dirname(DB_PATH), "indexing_queue.json")

# Site-wide "blind mode" flag (see api/core/blind_mode.py) — deliberately a
# single shared flag next to DB_PATH/INDEXING_QUEUE_PATH, not per-user like
# DOWNLOADS_DB_PATH: it's meant to affect every page load for everyone
# hitting this deployment while it's on, not just the admin who flipped it.
BLIND_MODE_PATH = os.path.join(os.path.dirname(DB_PATH), "blind_mode.flag")

# Latest health-check results (copied from tests/pvsuite results.json by
# pelicanvil_tests.sbatch after a standard-tier run) — same shared
# directory as DB_PATH, read by api/core/health_status.py for the
# home-page health widget visible to all users.
HEALTH_STATUS_PATH = os.path.join(os.path.dirname(DB_PATH), "health_status.json")
