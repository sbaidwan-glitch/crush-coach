# Running the Drive sync on Railway

The coach reads a copy of the team Drive kept in Postgres (`src/drive_index.py`).
Something has to keep that copy fresh. This is how to run it on Railway.

## What runs

| Job | Command | When |
|---|---|---|
| Keep it fresh | `python -m scripts.sync_drive --incremental` | a cron service, `*/30 * * * *` |
| First load, or a rebuild | `python -m scripts.sync_drive --full` | once, as its own service (below) |
| After changing how Sheets are read | `python -m scripts.sync_drive --full --reindex sheet` | once |
| The "Sync Drive now" button | an incremental sync, in the web service's background | `/admin`, leads only |

A full sync of the whole team Drive took about 16 minutes locally. Plan for an hour
or more on Railway (Docs/Sheets reads are paced to stay under Google's quota, and
Google sometimes rate-limits and retries). It must run as its own job, not from a
Railway shell session or a web request: both die when the connection or request does.

## Setup

1. **Web service** (already there). It runs `alembic upgrade head` on startup, so
   deploy it first after a PR that adds a migration. The sync jobs do not migrate.
2. **A second service from the same repo** (Settings → Source: same repo and
   Dockerfile) for the cron job:
   - Custom start command: `python -m scripts.sync_drive --incremental`
     (one command, no `&&`: with a Dockerfile the start command has no shell)
   - Cron schedule: `*/30 * * * *`
   - Variables: `DATABASE_URL` (reference the Postgres service's variable),
     `GOOGLE_SERVICE_ACCOUNT_JSON`, and optionally `DRIVE_INDEX_EXCLUDE`.
     Never put these in a file.
3. **The first load.** Nothing to do: until one full sync has finished, an
   `--incremental` run does a full one. So the first cron tick starts the load.
   To start it right away, trigger the cron service's run from the Railway dashboard
   (or `railway cron run`, see `railway cron --help`).

If you would rather keep the first load separate, add a third service with start
command `python -m scripts.sync_drive --full`, **no cron schedule**, and restart
policy "On Failure" with a few retries. Deploy it once and delete it when done.

## If it stops partway: it resumes

A stopped sync is safe to start again, and it picks up where it left off:

- Every file is saved as soon as it is read. A restart skips files that are already
  done (no Google call for them), and only re-lists the Drive (a minute or two).
- The "up to date" marker (the Drive changes token) is saved only when a full sync
  finishes. So until then, the 30-minute `--incremental` job keeps running a full
  sync, which means a stopped load is resumed within 30 minutes with no one
  touching it.
- The sync holds a lock so two never overlap. A running sync refreshes that lock
  every 30 seconds; a lock nobody has refreshed for 15 minutes belongs to a dead
  process and is taken over. A normal stop (Ctrl-C, SIGTERM on redeploy) releases
  it at once. After a hard kill (out of memory) expect up to 15 minutes before a
  restart can start; "skipped: another sync is running" in the log means that.
- Progress shows in the logs about once a minute
  (`Drive index: 12,000 of 40,000 files checked (3,400 read, 0 errors)`), and the
  counts on `/admin` update while it runs.
- Exit codes: 0 done (or skipped because another sync is running), 1 the whole sync
  failed (a restart policy of "On Failure" will retry it), 2 bad arguments.

Cron overlap: Railway skips a scheduled run while the previous one is still
running, and the lock above covers the cases where it doesn't.

## What gets left out

`src/drive_index_exclude.txt` lists folders, file names and file types the index
skips (code repositories, tool caches, photos, video, audio). Add more without a
deploy in the `DRIVE_INDEX_EXCLUDE` variable (rules separated by `;` or new lines):
see the header of `src/drive_index_exclude.txt` for the syntax. To see what is
worth excluding: `python scripts/index_stats.py --noise`.

A new rule applies to files already indexed at the next **full** sync (it removes
them); an incremental sync only applies rules to what changed.

## Size and cost

Check the real numbers any time with `python scripts/index_stats.py`. For the team
Drive in November 2026: about 48,000 files and folders and 4,100 text chunks made a
62 MB database before the exclude rules (the exclude rules leave about 7,000 rows).
Railway charges volumes at $0.15 per GB-month, so storage is cents a month, and a
Hobby volume starts at 5 GB. Postgres compute (memory, CPU) is usage-based and is
the real cost: check the project's usage page after the first week.
