"""Tracks which Discord objects the setup script manages, by stable key."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "filmbuffs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_objects (
    key         TEXT PRIMARY KEY,      -- e.g. channel:movies, tag:movies:backlog
    kind        TEXT NOT NULL,
    discord_id  INTEGER NOT NULL,
    extra       TEXT,                  -- JSON for kind-specific data
    updated_at  TEXT NOT NULL          -- UTC ISO-8601
);
"""


class State:
    def __init__(self, path: Path = DB_PATH):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def get(self, key: str) -> int | None:
        row = self.db.execute(
            "SELECT discord_id FROM managed_objects WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def get_extra(self, key: str) -> dict:
        row = self.db.execute(
            "SELECT extra FROM managed_objects WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row and row[0] else {}

    def set(self, key: str, kind: str, discord_id: int, extra: dict | None = None) -> None:
        if extra is None:
            extra = self.get_extra(key)
        self.db.execute(
            "INSERT INTO managed_objects(key, kind, discord_id, extra, updated_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "kind=excluded.kind, discord_id=excluded.discord_id, "
            "extra=excluded.extra, updated_at=excluded.updated_at",
            (key, kind, discord_id, json.dumps(extra),
             datetime.now(timezone.utc).isoformat()))
        self.db.commit()

    def all_ids(self) -> set[int]:
        return {r[0] for r in self.db.execute("SELECT discord_id FROM managed_objects")}
