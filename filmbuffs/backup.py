"""Nightly SQLite backups of data/filmbuffs.db into backups/.

Uses SQLite's online backup API, so a backup is a consistent snapshot even while
the bot (or backfill.py) is writing. Each backup is written to a .partial file,
checked with PRAGMA quick_check, then renamed into place. Files are named by
their UTC time, e.g. filmbuffs-20261112T090000Z.db.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

PREFIX = "filmbuffs-"
_NAME_RE = re.compile(r"^filmbuffs-(\d{8}T\d{6}Z)\.db$")
_STAMP = "%Y%m%dT%H%M%SZ"


def backup_name(when: datetime) -> str:
    return f"{PREFIX}{when.astimezone(timezone.utc).strftime(_STAMP)}.db"


def parse_backup_time(name: str) -> datetime | None:
    """UTC time of a backup file name, or None if it isn't one of ours."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), _STAMP).replace(tzinfo=timezone.utc)


def backups_to_prune(names: list[str], now: datetime, keep_days: int) -> list[str]:
    """Backups older than keep_days. The newest backup is always kept, even if old
    (e.g. after the bot was down for weeks). Files that aren't ours are ignored."""
    dated = sorted(((t, n) for n in names if (t := parse_backup_time(n)) is not None),
                   reverse=True)
    cutoff = now - timedelta(days=keep_days)
    return sorted(n for t, n in dated[1:] if t < cutoff)


def latest_backup(dest: Path) -> tuple[datetime, Path] | None:
    if not dest.is_dir():
        return None
    dated = [(t, p) for p in dest.iterdir() if (t := parse_backup_time(p.name))]
    return max(dated) if dated else None


def run_backup(src: Path, dest: Path, keep_days: int,
               now: datetime | None = None) -> tuple[Path, list[str]]:
    """Back up src into dest and prune old backups. Blocking; run it in a thread.
    Returns (new backup path, names pruned)."""
    now = now or datetime.now(timezone.utc)
    dest.mkdir(parents=True, exist_ok=True)
    final = dest / backup_name(now)
    partial = final.with_name(final.name + ".partial")
    partial.unlink(missing_ok=True)

    source = sqlite3.connect(src, timeout=30)
    try:
        target = sqlite3.connect(partial)
        try:
            source.backup(target)
            # A WAL-mode source copies the WAL flag; keep the backup a single file.
            target.execute("PRAGMA journal_mode=DELETE")
            result = target.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"backup failed quick_check: {result}")
        finally:
            target.close()
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        source.close()
    os.replace(partial, final)

    # Leftover .partial files from a crash mid-backup are ours to clean up.
    for p in dest.glob(f"{PREFIX}*.db.partial"):
        p.unlink(missing_ok=True)
    pruned = backups_to_prune([p.name for p in dest.iterdir()], now, keep_days)
    for name in pruned:
        (dest / name).unlink(missing_ok=True)
    return final, pruned
