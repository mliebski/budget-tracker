"""Small schema changes for databases created by an older version.

New tables are made by create_all(); a new column on an existing table needs an
ALTER TABLE. Each step is applied only when its column is missing, so this is
safe to run on every start, on MariaDB and SQLite alike.
"""
import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

log = logging.getLogger("budget")

# (table, column, column definition)
COLUMNS = [
    ("assets", "growth_rate", "growth_rate NUMERIC(6, 2)"),
]


def add_missing_columns(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, column, ddl in COLUMNS:
        if table in tables and column not in {c["name"] for c in insp.get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
            log.info("Added column %s.%s", table, column)
