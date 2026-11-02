# Public catalog endpoint + submission/approval workflow — investigation and design

Status: **investigation + plan only. No application code was changed.**
Branch at time of writing: `tests/anvil-suite`.

Things I could not verify from the repo are marked **[UNVERIFIED]** and listed again in section 4.

---

## 1. Investigation findings

### 1.1 Where categories/datasets live, and the schema

Single shared SQLite file, `DB_PATH` = `/anvil/projects/x-cda090008/anvil_reu26_pj4/pelican-ui-shared/pelican.db` (`api/core/config.py:3`; the old scratch path is left commented out on line 4).

Base schema, `scripts/init_db.py:6-26`:

```sql
CREATE TABLE IF NOT EXISTS datasets (
  id INTEGER PRIMARY KEY,
  name TEXT, description TEXT,
  path TEXT UNIQUE,            -- only constraint besides PK
  format TEXT, streamable INTEGER, access TEXT,
  tags TEXT                    -- JSON-encoded list[str]
);
CREATE TABLE IF NOT EXISTS categories (
  name TEXT UNIQUE,
  url  TEXT PRIMARY KEY,       -- slug; the real key. No integer id.
  icon TEXT,                   -- legacy static-file path
  description TEXT
);
CREATE TABLE IF NOT EXISTS authorizedUsers (name TEXT UNIQUE);
```

Columns added lazily at runtime (not in `init_db.py`, so a fresh DB built from `init_db.py` alone lacks them until first use):

- `categories.icon_data BLOB`, `icon_content_type TEXT`, `icon_updated_at TEXT`. Added by `ensure_icon_columns()` (`api/core/category_icons.py:33-62`) via try-ALTER/ignore. Called at **every app start** (`main.py:33-36` → `scripts/migrate_category_icons.py:63`) and **on every icon GET** (`api/routes/dataset.py:132`).
- `dataset_folder_sizes`, `dataset_folder_sizes_staging`: created lazily by `scripts/indexing_worker.py` (staging DDL ~line 393). Read by `_indexed_sizes_by_path` (`dataset.py:14-30`).

Indexes: only the implicit ones from `PRIMARY KEY` / `UNIQUE` (`datasets.id`, `datasets.path`, `categories.url`, `categories.name`, `authorizedUsers.name`). No explicit indexes. Fine at this size.

Other facts that matter for the design:

- **No FK between datasets and categories.** Membership = any dataset tag equal to a `categories.url` (`dataset.py:73-82`, comment says the same).
- No status, ownership, or timestamp columns anywhere. A dataset row is live the moment it's inserted.
- Datasets are keyed on `path` (federation path), which is the natural dedupe key for "add an endpoint".
- `data/datasets.json` / `data/categories.json` and the `DATASET_PATH`/`CATEGORY_PATH` constants (`dataset.py:9-11`) look like a legacy pre-SQLite source. Not read by any route I found. I did not check whether they are stale.

### 1.2 Routes that serve this data today

All in `api/routes/dataset.py`, all **GET, no auth check, no params, no pagination**:

| Route | Lines | Response |
|---|---|---|
| `GET /retrieve-datasets` | 33-56 | `[{id, name, description, path, format, streamable:bool, access, tags:[str], indexed_size_bytes:int\|null}]` (full `SELECT *`, plus a second query for sizes) |
| `GET /retrieve-categories` | 102-119 | `[{name, url, icon, description}]` |
| `GET /datasets/catalog` | 58-100 | `{dataset_count, category_count, category_names:[str], datasets:[{id, name, path, categories:[str]}]}` via `buildCatalog()` |
| `GET /categories/{url}/icon` | 122-151 | image bytes; 404 if none. Comment calls it "Public" |

Internal callers (these must not break):

- `retrieve-datasets` and `retrieve-categories` ← `datasets.js:130-131`; `retrieve-categories` ← `categories.js:14`.
- `/datasets/catalog` ← `docs.js:145` and `buildCatalog()` is called directly by the `/` page handler (`main.py:79`). The homepage therefore does 2 SELECTs per page load, and **a locked DB is a 500 on the home page**.

"Public" in the application code means only "no `is_authorized` check". See 1.5 for what actually gates access. Nothing is scoped to a logged-in session at the app layer: the app has no session concept.

### 1.3 SQLite connection management and concurrency

- **No shared connection and no pool.** Every handler does `sqlite3.connect(DB_PATH)` and `close()` (e.g. `dataset.py:36`, `database.py:43`, `auth.py:5`). `is_authorized` opens a connection on every admin call.
- **No PRAGMAs on the catalog DB.** No `journal_mode`, no `busy_timeout`. Only `downloads.py:43-47` sets WAL + `timeout=10`, and that is a different file, `DOWNLOADS_DB_PATH` in `~/.pelican-ui/`, which is per-user and local. So the catalog DB is in the default rollback-journal mode, and the app's catalog connections use Python's default `timeout=5.0`.
- The indexing worker uses `timeout=30` (`indexing_worker.py:390`) and says so explicitly: "pelican.db is genuinely multi-writer (this worker plus every PUN process's own admin-CRUD writes)", on "GPFS-backed shared storage" (`indexing_worker.py:373-389`). It also records a real incident on 2026-08-31 where the 5 s default produced `database is locked` mid-flush.
- **What happens today on a write/read collision:** in rollback-journal mode, a writer committing takes an exclusive lock that blocks readers. A reader's `connect()` waits up to 5 s, then raises `sqlite3.OperationalError: database is locked`. None of the read routes catch it (`serveDatasets`, `serveCategories`, `buildCatalog`, `serveCatalog` have no try/except; only `_indexed_sizes_by_path` catches `OperationalError`, and it is there to cover a missing table). That is an unhandled 500. The indexing worker's `_finalize_folder_sizes` is one big transaction and can hold the write lock for a long time (per the comment above). So **the existing GET routes, and the home page, can already 500 during indexing flushes.** This is the direct motivation for a fault-tolerant public endpoint.
- Other writes with no retry: `migrate_category_icons()` writes to the shared DB on **every PUN start** (`main.py:33`, `migrate_category_icons.py:113`), and `ensure_icon_columns` issues ALTERs on every icon GET. I'd expect the failing ALTER not to take a write lock, but I haven't verified that on this SQLite build. **[UNVERIFIED]**
- **WAL caveat, which matters for fault-tolerance design:** SQLite's own docs say WAL does not work over network filesystems (it needs shared memory in the `-shm` file). The repo comments say the DB sits on GPFS. Turning on `PRAGMA journal_mode=WAL` for `pelican.db` could cause corruption, and whether it is safe for this mount is a question for RCAC. I would not enable it without confirmation. **[UNVERIFIED: filesystem type and WAL safety for `/anvil/projects/...`]**
- The `_db_write_lock` in `downloads.py:22` is per-process and per-downloads-DB. It is not relevant to the catalog.

### 1.4 Admin / auth concept

An admin concept exists, but it is **not per-request identity.**

- `api/auth.py:4-10`: `is_authorized(user)` = `SELECT 1 FROM authorizedUsers WHERE name = ?`.
- Every router does `USER = os.environ.get("USER")` once at import (`main.py:22`, `database.py:12`, `indexing.py:10`, …) and calls `is_authorized(USER)` (e.g. `database.py:41`, `main.py:106`). `USER` is **the OS account that launched this Passenger process, not the person making the HTTP request.**
- This works only because OOD runs one Per-User Nginx (PUN) and one Passenger app instance per user; only that user's browser is proxied to that process (the codebase says "PUN" and "multi-process-per-user deployment" in `main.py:27-30`, `pelican_auth.py:5-7`). In practice: "a user is admin iff the account running their own copy of the app is in `authorizedUsers`."
- Consequences for this feature:
  - **There is no request-level identity to attach to a submission.** `submitted_by` has to be the `USER` env var of the PUN that received it, which is trustworthy only because OOD authenticated that user to that PUN.
  - **No client other than a browser with a valid OOD session can reach any instance** (see 1.5). A "public endpoint" is therefore not something the app can provide by itself.
  - The admin review queue works naturally across instances, because the shared `pelican.db` is the rendezvous: user A's PUN writes a pending row, admin B's PUN reads it. No cross-process networking is required.
  - Admin list/mutate routes are all `/admin/*` + `is_authorized(USER)`. The new admin review routes should follow the same convention and sit in `database.py`'s style.
- Admin UI is `api/templates/admin.html` + `api/static/js/admin.js`, with three selection boxes (`dataset-selection`, `category-selection`, `auth-users-selection`). The shared fetch wrapper is `adminRequest` (`admin.js:~700`). A review queue would be a fourth panel.

### 1.5 Network exposure via Open OnDemand on Anvil

What the repo shows:

- `manifest.yml` (`category: RCAC Staff`, `icon: fa://database`) and `passenger_wsgi.py` (`ASGIMiddleware(app)` from a2wsgi) mean this is a **Passenger app launched inside the user's PUN, not a Slurm batch-connect (interactive) job.** The README's checkout path is `~/ondemand/dev/pelicanvil`, which makes this a *dev app* served at `/pun/dev/<app>/` on the OOD host. `request.scope['root_path']` is plumbed everywhere (`ROOT_URL`, `window.ROOT_PATH`) because of that prefix.
- `git_info.py:21-25` and `.gitignore:20-23` confirm Passenger conventions (`tmp/restart.txt`, `tmp/`, `log/`).
- The README's "Known limits" mention Passenger process recycling/idle-timeout termination as something the suite cannot test.
- There is **no reverse proxy config, nginx/apache config, auth middleware, CORS config, or forwarded-header handling in the repo.** The only thing in front of the app is OOD's own Apache + per-user Nginx, configured outside this repo.

What that implies, and what is a definitive answer vs. not:

- **Definitive from the code:** the app serves one URL prefix per user, on OOD's host, through OOD's proxy chain. There is nothing in the repo that would give it a separate public ingress. So the answer to "is there an existing stable ingress other than OOD's proxy" is **no, not in this repo.**
- **The URL is stable in the sense that matters, but it is not "public".** It is not a job port that changes on relaunch (that's batch-connect `/node/<host>/<port>/`), so a requeue doesn't change it. The Passenger process can be started on demand by the next request and can be torn down when idle or when the user's PUN is stopped. But `/pun/dev/pelicanvil/...` is only served by the **requesting user's own PUN**, and OOD's Apache requires an authenticated OOD session. Dev apps are, by OOD convention, visible only to the owning user. **[UNVERIFIED: Anvil's exact OOD auth config (I expect Purdue SSO via mod_auth_openidc) and whether any unauthenticated path exists. This needs one `curl -i` of the URL from outside the browser session, and a question to RCAC.]**
- **Therefore: an arbitrary external client sending a GET cannot reach this today, and cannot be made to reach it by anything in the app.** Even with OOD sessions, user X's browser/session talks to X's own PUN; it can't call Y's PUN. The "public catalog endpoint" requirement can only be met in one of these ways:
  1. **Within OOD:** every authenticated Anvil/Purdue user gets the same JSON from their own instance of the app (`/pun/sys|dev/<app>/api/v1/catalog`). Any script that carries a valid OOD session cookie works (awkward for non-browser clients). The URL is per-user-instance but the content is identical because it's served from the shared DB. This is deliverable now.
  2. **Outside Anvil/OOD (needs infra):** a snapshot file published somewhere with a stable URL (static hosting, a GitHub repo/Pages, a Purdue web host, RCAC-provided service endpoint). The app, on approval, writes the snapshot; a separate publisher pushes it. This is the only way to have something truly "public" and durable without depending on any user's PUN. **This is a decision for your mentor/PM** (see section 4).
- Network gating: nothing in the code distinguishes on-campus/VPN from off-campus. Whether Anvil OOD is reachable off-VPN is a facility fact, not in the repo. **[UNVERIFIED]**

### 1.6 Rate limiting, validation, abuse protection, caching

- **Rate limiting: none** (no middleware, no `slowapi`, no dependency in `pyproject.toml:7-15`). The `ThreadPoolExecutor(max_workers=3)` in `downloads.py:29` is a concurrency cap on downloads, not request limiting.
- **Validation: pydantic type-checking only** (`DatasetCreate`, `CategoryCreate`, `database.py:16-37`). No length limits, no regex on `path`/`url` slug, no allowed-values for `format`/`access`, no URL/path normalisation. Trust is justified today only because every writer is an authorised admin.
- **Sanitisation / XSS: absent on the frontend.** Cards are built with `innerHTML` and template strings with unescaped fields, e.g. `datasets.js:338` (`${dataset.name}`), `:342` (`${dataset["format"]}`), `:346` (`${dataset["access"]}`). Only `downloads.js` has any escape helper (8 hits). `innerHTML` appears 148 times across 8 JS files. **Once non-admins can create rows that become public, a submitted name like `<img src=x onerror=...>` becomes stored XSS, running in the OOD origin for every user who opens the datasets page.** Server-side validation alone is not enough; the render sites must escape. This is the biggest hidden cost in the feature.
- **Caching: none.** No `Cache-Control`, `ETag`, `Last-Modified` or in-memory cache on any catalog route; the icon route (`dataset.py:151`) also sets none. Client JS uses `cache: "no-store"` only on job polling. `StaticFiles` provides ETag/Last-Modified for `/api/static` only. Computed once at startup: `git_version` (`main.py:42`).
- `/admin/*` mutation routes use POST with query params for ids (e.g. `?dataset_id=`), no CSRF protection. That's tolerable today because the OOD session is the only credential, but it's worth noting for the new admin routes.

---

## 2. Design

### 2.1 Schema

**Recommendation: a separate `submissions` table. Do not add a status flag to `datasets`/`categories`.**

Reasons:

1. **Every existing reader assumes every row in `datasets` is live.** `serveDatasets`, `buildCatalog`, `_get_dataset_path`, the homepage counts, the indexing queue (auto-enqueue on insert, `database.py:74`), `retrieve-datasets` consumed by `datasets.js`. A `status` column would force a `WHERE status='approved'` onto every one of those, and any one missed leaks unreviewed rows into the UI. A forgotten filter would silently publish; a separate table fails closed.
2. **Pending/rejected rows would collide with the live uniqueness constraint.** `datasets.path` is `UNIQUE`; a rejected submission of a path would block resubmission or a later legitimate add. Separate tables avoid it.
3. **Different lifecycle and different data.** Submissions need `submitted_by`, `reviewed_by`, timestamps, `rejection_reason`, an audit trail, and probably the *proposed* payload verbatim (including possible admin edits). None of that belongs on a live catalog row.
4. **Approval becomes one clear atomic operation**: insert into `datasets` + mark submission approved, in one transaction (2.3).
5. The cost: one copy step on approval and a bit of duplicated columns. I think that's cheap.

Proposed DDL (new, created lazily in the same style as `ensure_icon_columns`, plus added to `init_db.py` so test DBs get it):

```sql
CREATE TABLE IF NOT EXISTS submissions (
  id            INTEGER PRIMARY KEY,
  kind          TEXT NOT NULL CHECK (kind IN ('dataset','endpoint')),  -- see note
  -- proposed payload, mirrors datasets columns
  name          TEXT NOT NULL,
  description   TEXT NOT NULL,
  path          TEXT NOT NULL,
  format        TEXT, streamable INTEGER NOT NULL DEFAULT 0, access TEXT,
  tags          TEXT NOT NULL DEFAULT '[]',
  -- workflow
  status        TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','approved','rejected')),
  submitted_by  TEXT NOT NULL,          -- OS $USER of the submitting PUN
  submitted_at  TEXT NOT NULL,          -- ISO-8601 UTC
  reviewed_by   TEXT, reviewed_at TEXT,
  rejection_reason TEXT,
  approved_dataset_id INTEGER           -- set on approval; links to datasets.id
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions(status, submitted_at);
-- one open submission per path at a time:
CREATE UNIQUE INDEX IF NOT EXISTS idx_submissions_pending_path
  ON submissions(path) WHERE status = 'pending';
```

Notes:

- `kind`: "dataset" vs "Pelican/OSDF endpoint". In today's schema an endpoint *is* a dataset row (a federation `path`; Quick Access is the separate ad-hoc path browser). I'd keep a single payload shape and use `kind` only as a label until you tell me endpoints need distinct fields. **Question for you** (section 4, Q3).
- `submitted_by` is the PUN's `$USER`, not a verified request identity (see 1.4), which is fine for audit.
- Category submissions: out of scope for v1. The goal text says "dataset or Pelican/OSDF endpoint". Categories are few, admin-curated, and carry icon blobs. Dataset submissions can *reference* existing category slugs in `tags`.
- **Changes to `datasets`:** add `created_at TEXT`, `source_submission_id INTEGER` (both nullable, via try-ALTER). Optional but useful for provenance and for a "since" filter later. No change to `categories`.
- Add `catalog_meta(key TEXT PRIMARY KEY, value TEXT)` holding `catalog_version` (integer). See 2.3.

### 2.2 Routes

New router file `api/routes/catalog.py`; existing routes unchanged.

**A. Public catalog: `GET /api/v1/catalog`** (path chosen so it doesn't collide with the existing `/datasets/catalog`, which has a different, thinner shape and a different consumer)

- Auth: none at app level (matching existing read routes). Reachability is whatever OOD gives (1.5).
- Response:
  ```json
  {
    "catalog_version": 42,
    "generated_at": "2026-10-14T18:03:11Z",
    "stale": false,
    "dataset_count": 123,
    "categories": [{"name": "...", "url": "...", "description": "...", "icon_url": "categories/<url>/icon"}],
    "datasets": [{"id": 1, "name": "...", "description": "...", "path": "...", "format": "...", "streamable": true, "access": "public", "tags": ["..."], "categories": ["climate"], "indexed_size_bytes": null}],
    "next_cursor": null
  }
  ```
  `icon_data` blobs are never inlined. `stale` is `true` when serving last-good after a failed refresh (2.3).
- Only `datasets` rows exist in the live table, and anything in there is by definition approved, so "only returns approved rows" is satisfied structurally. I'm deliberately **not** filtering on a status column (that would reintroduce the leak risk from 2.1).
- Pagination: not at current size. Existing endpoints return everything, and the catalog is O(100s) of rows. Include `limit` (default 500, max 1000) + opaque `cursor` (last `id`) from day one, because adding it later is a breaking change. The cursor needs the `next_cursor` field above to be reserved now.
- Conditional GET: `ETag: "<catalog_version>"`, `Last-Modified`, `Cache-Control: public, max-age=60, stale-while-revalidate=300`. Honour `If-None-Match` → 304.
- Never returns 5xx for a DB problem if any last-good snapshot exists (2.3). If there is *no* snapshot yet and the DB is unreadable: 503 with `Retry-After`.
- Per-user instance note: because each PUN is a separate process, "in-memory last-good" is per process. See 2.3.

**B. Public submission: `POST /api/v1/submissions`**

- Auth: any request that reaches the app (i.e. any authenticated OOD user). **Not** gated on `is_authorized`. `submitted_by = $USER`.
- Body: `name` (1-120), `description` (1-2000), `path` (federation path, must match `^/[A-Za-z0-9._~+/ -]{1,512}$`, no `..`, normalised without trailing slash, to match how `indexed_sizes` is keyed at `dataset.py:53`), `format` (≤40 or enum), `streamable` (bool), `access` (enum `public`/`protected`; confirm real values **[UNVERIFIED]**), `tags` (≤20 items, each a lowercase slug ≤40), `kind`. Use pydantic `Field` constraints and strict model config (reject unknown fields).
- Dedupe: reject (409) if `path` already exists in `datasets` or has a pending submission (the partial unique index enforces the latter race-free).
- **Anti-spam, scoped to what's realistic here:**
  - Per-`submitted_by` cap on open pending submissions (e.g. 5) and per-day cap, enforced by a COUNT inside the same insert transaction. This is the effective limiter, since identity is trustworthy (OOD-authenticated).
  - Body size limit (reject over ~16 KB) before parsing.
  - A per-process in-memory token bucket is not meaningful here, since each user has their own process. Don't build one.
  - Optional: federation sanity check that `path` resolves (HEAD/listing via the existing pelicanfs code in `pelican.py`) at *review* time, not submit time, to avoid making submit slow and to avoid turning the endpoint into a federation probe.
- **Sanitisation:** store as given after validation; **do not** try to strip HTML server-side. Instead escape on output (next bullet).
- **Prerequisite, not optional:** add an `escapeHtml` helper and apply it at every `innerHTML` render of catalog fields in `datasets.js`, `categories.js`, `quick-access.js`, `admin.js` (see 1.6) before any user-submitted row can reach `datasets`. Cheapest low-risk shape: one shared `escapeHtml` in a small static JS file, used at the templated sites that interpolate catalog fields. Admin-queue rendering must escape too, since reviewers are the first people to see raw submissions.
- Response: `201 {id, status:"pending"}`.
- `GET /api/v1/submissions/mine` (read-only, own rows by `$USER`) so submitters can see status and rejection reasons. Cheap and avoids "did it work?" support questions.

**C. Admin review routes** (all `is_authorized(USER)` → 403 otherwise, same as `database.py`; POST bodies are JSON, not query params)

- `GET  /admin/submissions?status=pending&limit=&cursor=`: list.
- `GET  /admin/submissions/{id}`: detail, plus the duplicate check result.
- `POST /admin/submissions/{id}/approve`: optional JSON `{"edits": {name?, description?, path?, format?, streamable?, access?, tags?}}`. Edit-before-approve is cheap because the approve handler already re-validates the payload through the same pydantic model; I recommend including it (admins will want to fix tags/typos without a reject-resubmit loop). The stored submission keeps the original payload; the final inserted values are what's in `datasets`. Record that edits occurred (`edited_by_reviewer` flag or store edited JSON), your call.
- `POST /admin/submissions/{id}/reject`: `{"reason": "..."}` (required, ≤500).
- Self-approval: allow or forbid `reviewed_by == submitted_by`? Default: allow, since the admin set is tiny. Flag as a policy choice.
- On approval, enqueue indexing exactly as `addDataset` does (`database.py:74-77`), best-effort and *after* commit.

**Auth summary**

| Route | Who can call | How enforced |
|---|---|---|
| `GET /api/v1/catalog` | anyone who can reach the app. Today = any user with a valid OOD session, hitting their own PUN. Truly external = needs infra (section 4) | no app-level check (consistent with existing routes) |
| `POST /api/v1/submissions`, `GET /api/v1/submissions/mine` | same reachability; identity = PUN's `$USER` | OOD authentication + per-user caps |
| `/admin/submissions/**` | admins | `is_authorized(USER)` (same pattern, same limits: admin = account running this PUN) |

### 2.3 Fault tolerance (the core)

**Read path: serve from a snapshot, not live SQLite. Reasons first, then trade-offs.**

What I found that drives this: catalog DB is on shared (GPFS-like) storage, rollback-journal mode, 5 s default timeout, multiple writers (indexing worker with long transactions, every PUN's admin CRUD and its startup migration), and nothing on the read path catches `database is locked` (1.3). Traffic is low and read-mostly, data is tiny, and writes (approvals) are rare and admin-initiated. That profile is ideal for snapshot-and-serve.

Design:

1. **Snapshot = a JSON document + `catalog_version`**, built by a single function `build_snapshot(con)` that reads `datasets`, `categories` and `dataset_folder_sizes` inside **one read transaction** (consistent view).
2. **Two layers of last-good:**
   - *In-memory per process* (dict + `threading.Lock`; replaced atomically by swapping a reference). Served first.
   - *On-disk shared snapshot file* `catalog_snapshot.json` next to `DB_PATH` (like `indexing_queue.json` and `blind_mode.flag` already are, `config.py:17,23`). It's written by `os.replace` of a temp file (same atomic-publish idiom the indexing queue uses; the log line "queue file atomic write" confirms this exists). Every PUN reads it, so a freshly started PUN (or one that can't read SQLite) still has last-good. Add `CATALOG_SNAPSHOT_PATH` to `api/core/config.py`; the test launcher patches config paths (README "Isolation" section), so it will need adding to the patched list.
3. **Request handling:** serve memory snapshot if its `catalog_version` is current. "Current" is cheaply checked by comparing against the snapshot file's mtime/`catalog_version` (a `stat()` plus tiny read), not by querying SQLite. If the file is newer, reload it. If anything fails, serve what we have with `"stale": true` and `X-Catalog-Stale: 1`. Only if no snapshot exists anywhere: try a one-off live build with a short timeout (`connect(timeout=2)`); if that fails → `503` + `Retry-After: 30`.
4. **Refresh triggers:**
   - On every approval, synchronously after the commit (2.3 atomicity), rebuild + publish the snapshot file. If the rebuild fails, the approval stands (data is committed) and the snapshot is marked dirty; the next refresh fixes it.
   - Existing admin dataset/category CRUD (`add/modify/remove-*`, icon upload) should also bump the version and refresh, otherwise the snapshot goes stale when an admin edits directly. That's a small hook in `database.py`; it's the one place the new feature touches existing code beyond adding routes.
   - Periodic fallback: a lazy check in the GET path ("if snapshot older than N=300 s, attempt a rebuild in a background thread; on failure keep serving last-good"). No scheduler is needed; Passenger can idle-kill processes, so don't rely on a timer thread.
   - Indexing worker finishing a dataset changes `indexed_size_bytes`. It doesn't need to bump `catalog_version` for correctness, so the periodic refresh picks sizes up within N seconds. (Optionally the worker bumps the version too. Skip for v1.)
5. **Trade-off vs reading SQLite live:**
   - Live read: always fresh, simplest code, zero new state. But it inherits every problem in 1.3 (5 s lock waits, 500s during indexing flushes) and ties public availability to DB health. Bounded-retry live reads (`timeout=…`, 2-3 attempts) reduce but don't remove that; and a *read-only* `file:...?mode=ro` connection still needs the lock in rollback mode.
   - Snapshot: bounded staleness (≤ approval latency or ≤ N seconds on fallback), one more file to keep coherent, and a cache-invalidation hook on every write path. Given the incident record and the multi-writer setup, I judge the extra moving part to be worth it. If the snapshot file turns out to be a hassle, the minimum viable variant is in-memory last-good only with a live read with 2 s timeout, which still removes most 500s.
   - Not recommended: enabling WAL on `pelican.db` to fix this (1.3, WAL on network FS).

**Atomicity of approval** — one SQLite transaction, `BEGIN IMMEDIATE` (takes the write lock up front so it can't deadlock-upgrade), `busy_timeout` ≥ 30 s like the worker:

```
BEGIN IMMEDIATE;
  SELECT ... FROM submissions WHERE id=? AND status='pending';     -- re-check inside the lock
  INSERT INTO datasets (...) VALUES (...);                          -- UNIQUE(path) is the final dedupe guard
  UPDATE submissions SET status='approved', reviewed_by=?, reviewed_at=?, approved_dataset_id=last_insert_rowid()
        WHERE id=? AND status='pending';                            -- rowcount must be 1
  UPDATE catalog_meta SET value = value + 1 WHERE key='catalog_version';
COMMIT;
```

A crash before COMMIT rolls back via SQLite's journal, so there is no half-approved state (no dataset without the status flip, no flip without the dataset). Two admins approving at once: the loser sees `status != 'pending'` or a UNIQUE violation and gets a 409. Only after COMMIT: publish snapshot, then best-effort enqueue indexing. A crash between COMMIT and snapshot-publish is healed by the periodic refresh (snapshot's `catalog_version` < DB's).

Snapshot publish is atomic by `os.replace` on the same filesystem, never write-in-place. (Confirm `os.replace` atomicity on the mount; the indexing queue already relies on it **[UNVERIFIED on GPFS]**.)

**Versioning** (concrete): integer `catalog_version` in `catalog_meta`, bumped inside every transaction that changes the catalog. Exposed three ways: JSON field, `ETag: "v<version>"` (strong; the body is deterministic for a version, except `generated_at` and `stale`, which I'd keep out of the ETag'd body or move to headers, otherwise the ETag lies), and `Last-Modified` = snapshot build time. Clients do `If-None-Match` → 304. A client detects staleness via `stale: true` / `X-Catalog-Stale` and by `generated_at`. Why an integer rather than a content hash: cheap to compare without reading the body, and monotonic, so clients can detect rollback.

Caveat: `indexed_size_bytes` changes don't bump the version in v1 (above), so an ETag can stay constant while sizes drift. Either include sizes in a content hash for the ETag, or accept it. I'd accept it and say so in the API docs.

**What happens when the host process dies**

- The app is a Passenger app inside the user's PUN (1.5), not a Slurm job, so **there is no "requeue" and no port change**. If the PUN/Passenger process is reaped (idle timeout, user logout, OOD restart, or `tmp/restart.txt`), the next authenticated request relaunches it; cold start takes seconds, and import-time work (`migrate_category_icons()`, `get_git_version_info()`, `_init_db()`) runs again. With the snapshot file on shared storage, a cold start can serve the catalog without touching SQLite (also: the startup migration write should be made non-fatal-and-quick; it already is best-effort).
- **But that is availability *per user, on demand*, not a public always-on service.** No user's PUN → no endpoint for that user; and nothing outside an OOD session can reach it at all. If "public" has to mean "an external client can hit a URL at any time without being logged into Anvil", the app on OOD cannot provide it. Realistic options, all needing someone else's say:
  - (a) Accept: "public" = any authenticated Anvil/Purdue user. Document the cold-start behaviour. No new infra.
  - (b) Publish the snapshot file to an external stable location on every approval (GitHub repo/Pages, Purdue web space, an RCAC-hosted service). The in-app endpoint stays, and the external copy is the true public one. Needs a decision, credentials/ownership, and probably RCAC approval.
  - (c) Ask RCAC whether Anvil has a hosted-service / persistent web endpoint offering. **[UNVERIFIED that it does]**
  - A status page makes no sense for (a) (it would be served by the same thing that's down). For (b), the published file's `generated_at` is the status.
- **I recommend building (a) first (it also delivers the admin workflow, which is the real value) and treating (b) as a follow-up decision.** The snapshot-file design makes (b) a ~20-line add-on (copy the file after publish).

### 2.4 Implementation order (each step is independently testable and shippable)

Tests go in the existing suite style (`tests/pvsuite/test_db.py::catalog_crud_through_admin_routes` is the template; it runs against a temp DB seeded by `init_db.py`).

1. **Escape all catalog fields on render (frontend only).** Add shared `escapeHtml`, apply at `innerHTML` sites for name/description/format/access/tags in `datasets.js`, `categories.js`, `quick-access.js`, `admin.js`. Zero backend risk, pure hardening, and a hard prerequisite for 5. Test: seed a dataset named `<img src=x onerror=...>` through the admin route and assert via a browser check (Playwright MCP is available locally) that no element is injected.
2. **Error-tolerant reads + shared connection helper.** `api/core/db.py`: `connect(timeout=…)`, `read_only` helper. Wrap `serveDatasets`/`serveCategories`/`buildCatalog` so a `database is locked` after retry becomes a 503 (not an unhandled 500) and `/` still renders. Behaviour-preserving otherwise. Test: hold a write lock from a second connection, assert 503 shape.
3. **Schema: `submissions`, `catalog_meta`, nullable `datasets.created_at`/`source_submission_id`**, lazy-created like `ensure_icon_columns` + added to `init_db.py`. Test: idempotent across restarts (extend `schema_init_is_idempotent`).
4. **Snapshot builder + `GET /api/v1/catalog`** with ETag/304, pagination params, memory + file last-good, `stale` flag, `catalog_version` bumps hooked into the existing admin CRUD. Test: 304 on repeat; after a direct admin add, version increments and new row appears; with SQLite locked or the DB path made unreadable, the endpoint still returns 200 `stale:true`; with no snapshot and locked DB, 503.
5. **Submission route** (`POST /api/v1/submissions`, `GET .../mine`): validation, dedupe, caps. Test: boundary lengths, bad paths (`..`), duplicates, over-quota, concurrent duplicate submission → exactly one wins.
6. **Admin review API** (list/detail/approve/reject/edit-before-approve) with the atomic transaction. Test: kill the process between statements (the suite already has `kill -9` machinery in `test_kill.py`) and assert no partial state; two concurrent approves → one 200, one 409; approval appears in `/api/v1/catalog` immediately; non-admin gets 403.
7. **Admin UI review queue** in `admin.html`/`admin.js` (fourth panel, uses `adminRequest`, escapes everything).
8. **Periodic-refresh fallback + docs** (API reference in `README`/`docs.html`, cold-start behaviour, `stale` semantics).
9. **(Only if approved)** external snapshot publishing.

PR boundaries: 1; 2; 3+4; 5; 6; 7; 8.

---

## 3. Smaller notes found along the way

- `serveCategoryIcon` calls `ensure_icon_columns` (DDL attempts) on a public GET. Worth removing once the migration has run, so a public read path never attempts DDL.
- `main.py:88` and `main.py:72` both define a function named `main_page` (the second silently shadows the name; routes still register correctly since they're registered at decoration time). Cosmetic.
- `pelican.py:579` registers `@pelicanRouter.get("datasets/")` without a leading slash. Not related, but looks unintended.
- `tests` launcher patches `DB_PATH`, `DOWNLOADS_DB_PATH`, `INDEXING_QUEUE_PATH`, `BLIND_MODE_PATH`; the new `CATALOG_SNAPSHOT_PATH` must be added there or a test run could write the real snapshot file next to the production DB.

---

## 4. Open questions / decisions needed

Top three to settle before implementation:

1. **What does "public" have to mean? (mentor/PM decision)** As far as I can tell from the repo, the app is a per-user Passenger app behind OOD's authenticated proxy, so *no* unauthenticated external client can reach it, and nothing in the app can change that. Either accept "any authenticated Anvil/Purdue user" (buildable now), or approve an out-of-Anvil publishing target for a snapshot (needs infra/ownership), or ask RCAC about a hosted endpoint. This is the thing most likely to invalidate the requirement as written. **To confirm in ~5 minutes before the meeting:** `curl -i` the app URL without cookies (expect a redirect to SSO), and ask RCAC whether any Anvil OOD path is reachable unauthenticated and off-VPN.
2. **Filesystem/locking facts for `/anvil/projects/...`.** Is it GPFS, and is it OK to stay on rollback-journal mode with 30 s busy timeouts (my plan) versus WAL (which I expect to be unsafe on a network FS)? Also confirm `os.replace` atomicity for the snapshot file on that mount. If RCAC says WAL is fine, the design simplifies (readers stop blocking on writers).
3. **Scope of "endpoint" vs "dataset", and review policy.** Is a Pelican/OSDF endpoint just a dataset row (what I assumed), or does it need its own fields (e.g. namespace/origin, token requirement)? And: may admins approve their own submissions, and should categories be submittable too (I excluded them)?

Other items to confirm: real allowed values for `datasets.access`/`format` (to turn into enums); whether `data/*.json` is still used by anything; whether to widen the ETag to cover indexing-size changes; whether direct admin CRUD should also be reflected in the catalog immediately (I assume yes, which requires the hook in step 4).
