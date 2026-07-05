"""
SQLite cache for scraped listings.

Caching matters here for two reasons:
  1. Politeness — don't re-scrape sgcarmart every time someone asks about
     a "2019 Honda Vezel" within the same day.
  2. Speed — the agent loop may call the search tool multiple times
     (e.g. widening year range); cached data makes retries instant.
"""

import sqlite3
import time
import json
from contextlib import contextmanager

import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_key TEXT NOT NULL,       -- normalized "make|model|year" search key
    title TEXT,
    price INTEGER,                 -- SGD, cleaned to integer
    reg_year INTEGER,
    coe_years_left REAL,
    mileage_km INTEGER,
    depreciation_per_year INTEGER,
    url TEXT,
    scraped_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_query_key ON listings(query_key);

CREATE TABLE IF NOT EXISTS search_meta (
    query_key TEXT PRIMARY KEY,
    last_scraped_at REAL NOT NULL,
    result_count INTEGER NOT NULL
);
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_column(conn, table: str, column: str, coltype: str):
    """Add a column to an existing table if it's not already there — lets
    pre-existing cache DBs pick up new fields without a manual reset."""
    cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "listings", "coe_years_left", "REAL")


def is_cache_fresh(query_key: str) -> bool:
    """Return True if we scraped this query recently enough to skip re-scraping."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT last_scraped_at FROM search_meta WHERE query_key = ?",
            (query_key,),
        ).fetchone()
    if not row:
        return False
    age_hours = (time.time() - row["last_scraped_at"]) / 3600
    return age_hours < config.CACHE_TTL_HOURS


def save_listings(query_key: str, listings: list[dict]):
    with get_conn() as conn:
        # Clear old entries for this query key before inserting fresh ones
        conn.execute("DELETE FROM listings WHERE query_key = ?", (query_key,))
        for item in listings:
            conn.execute(
                """INSERT INTO listings
                   (query_key, title, price, reg_year, coe_years_left, mileage_km,
                    depreciation_per_year, url, scraped_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    query_key,
                    item.get("title"),
                    item.get("price"),
                    item.get("reg_year"),
                    item.get("coe_years_left"),
                    item.get("mileage_km"),
                    item.get("depreciation_per_year"),
                    item.get("url"),
                    time.time(),
                ),
            )
        conn.execute(
            """INSERT INTO search_meta (query_key, last_scraped_at, result_count)
               VALUES (?, ?, ?)
               ON CONFLICT(query_key) DO UPDATE SET
                   last_scraped_at = excluded.last_scraped_at,
                   result_count = excluded.result_count""",
            (query_key, time.time(), len(listings)),
        )


def load_listings(query_key: str) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE query_key = ?", (query_key,)
        ).fetchall()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {config.DB_PATH}")
