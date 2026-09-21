"""Create a SQLite database using genuine baseline-v25 schema semantics."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from tests.fixtures import baseline_v25_database


def create_genuine_v25_database(path: Path | str) -> Path:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        baseline_v25_database.initialize_database(connection)
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 25:
            raise AssertionError(f"Genuine v25 fixture produced schema {version}")
        epoch_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'runtime_epoch%'"
            )
        }
        if epoch_tables:
            raise AssertionError(f"Genuine v25 fixture must not contain epoch tables: {epoch_tables}")
        return database_path
    finally:
        connection.close()
