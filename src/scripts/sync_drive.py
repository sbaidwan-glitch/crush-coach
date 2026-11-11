"""Sync the team Drive into the local index (for the Railway cron job).

Run from src/:

    python -m scripts.sync_drive --incremental   # what changed since last time
    python -m scripts.sync_drive --full          # walk the whole drive again
    python -m scripts.sync_drive --full --force  # also re-read unchanged files

Exit codes: 0 done (even if some files' text couldn't be read; those are listed),
1 the sync failed as a whole, 2 bad arguments. If another sync is already
running, this one skips and exits 0: overlapping cron runs are not an error.
"""

import argparse
import logging
import signal
import sys

from sqlmodel import Session

import db
import drive_index

log = logging.getLogger("crush_coach")


def _stop(signum, frame) -> None:
    """Turn a stop signal into a normal exit, so the sync lock is released."""
    raise SystemExit(128 + signum)


def install_signal_handlers() -> None:
    """Railway stops a job with SIGTERM; without this the lock would be left held.

    SIGHUP (terminal closed) is only handled if it isn't ignored: under `nohup`
    it is, and the sync should keep running then.
    """
    signal.signal(signal.SIGTERM, _stop)
    if signal.getsignal(signal.SIGHUP) != signal.SIG_IGN:
        signal.signal(signal.SIGHUP, _stop)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync the shared Drive index")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--full", action="store_true", help="walk the whole drive")
    mode.add_argument(
        "--incremental",
        action="store_true",
        help="apply the Drive changes since the last sync (a full sync the first time)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="with --full: re-read every file, even if it hasn't changed",
    )
    args = parser.parse_args(argv)
    if args.force and not args.full:
        parser.error("--force only works with --full")

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    install_signal_handlers()
    try:
        with Session(db.engine) as session:
            if args.full:
                report = drive_index.full_sync(session, force=args.force)
            else:
                report = drive_index.incremental_sync(session)
    except drive_index.SyncAlreadyRunning:
        print("Another Drive sync is already running; skipped.")
        return 0
    except drive_index.SyncError as exc:
        print(f"Drive sync failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001  (print one clean line, not a traceback)
        print(f"Drive sync failed: {type(exc).__name__}", file=sys.stderr)
        return 1

    print(
        f"{report.mode} sync done: {report.files_seen} files seen, "
        f"{report.files_read} read, {report.removed} removed, "
        f"{report.text_errors} with unreadable text"
    )
    for line in report.errors:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
