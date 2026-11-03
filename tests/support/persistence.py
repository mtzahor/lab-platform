from __future__ import annotations

from pathlib import Path

from lab_platform.persistence.database import SQLiteDatabase


def open_sqlite_database(path: Path) -> SQLiteDatabase:
    """Open and migrate a SQLite database file for a test."""
    database = SQLiteDatabase(path)
    database.initialize()
    return database
