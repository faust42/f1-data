"""
db_setup.py
Author: jacivell

Phase 1 of the F1 Research Database project.
Creates the SQLite database and all tables with proper relationships.
Run this once to initialize the database before fetching any data.

Usage:
    python db_setup.py
"""

import sqlite3
import os

# ── Configuration ─────────────────────────────────────────────────────────────

# Hardcoded to match your project folder on the VS Code server
DB_PATH = "/root/f1-data/f1_data.db"


# ── Table Creation ─────────────────────────────────────────────────────────────

def create_tables(conn):
    """
    Create all F1 database tables.

    LEARNING NOTE: We use a cursor to execute SQL statements.
    A cursor is like a pointer into the database — it lets us
    send commands and retrieve results back from SQLite.

    LEARNING NOTE: Foreign keys enforce relationships between tables.
    For example, every row in 'drivers' must reference a valid team_id
    in 'teams'. This prevents orphaned/invalid data from sneaking in.

    LEARNING NOTE: IF NOT EXISTS makes this script safe to re-run.
    It won't wipe your data if the tables already exist.
    """

    cursor = conn.cursor()

    # SQLite disables foreign key enforcement by default — turn it on
    # LEARNING NOTE: PRAGMA is SQLite's way of setting database-level options
    cursor.execute("PRAGMA foreign_keys = ON")

    # ── Teams ─────────────────────────────────────────────────────────────────
    # One row per F1 constructor (team).
    # constructor_id is the Ergast API slug (e.g. "red_bull", "mercedes")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            team_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            constructor_id  TEXT    UNIQUE NOT NULL,
            name            TEXT    NOT NULL,
            full_name       TEXT,
            nationality     TEXT,
            base            TEXT,
            first_season    INTEGER,
            championships   INTEGER DEFAULT 0,
            created_at      TEXT    DEFAULT (datetime('now'))
        )
    """)
    print("  ✓ teams table ready")

    # ── Drivers ───────────────────────────────────────────────────────────────
    # One row per driver. team_id = their current or most recent team.
    # ergast_id is the Ergast API slug (e.g. "hamilton", "verstappen")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS drivers (
            driver_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ergast_id       TEXT    UNIQUE NOT NULL,
            forename        TEXT    NOT NULL,
            surname         TEXT    NOT NULL,
            nationality     TEXT,
            date_of_birth   TEXT,
            driver_number   INTEGER,
            team_id         INTEGER,
            active          INTEGER DEFAULT 1,
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (team_id) REFERENCES teams(team_id)
        )
    """)
    print("  ✓ drivers table ready")

    # ── Seasons ───────────────────────────────────────────────────────────────
    # One row per driver per season — their full-year stats.
    # This lets us track how a driver performed across multiple teams/years.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS seasons (
            season_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            year            INTEGER NOT NULL,
            driver_id       INTEGER NOT NULL,
            team_id         INTEGER NOT NULL,
            points          REAL    DEFAULT 0,
            wins            INTEGER DEFAULT 0,
            podiums         INTEGER DEFAULT 0,
            poles           INTEGER DEFAULT 0,
            final_position  INTEGER,
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
            FOREIGN KEY (team_id)   REFERENCES teams(team_id),
            UNIQUE (year, driver_id)
        )
    """)
    print("  ✓ seasons table ready")

    # ── Races ─────────────────────────────────────────────────────────────────
    # One row per Grand Prix event.
    # round = race number within the season (e.g. round 5 of 24)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS races (
            race_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            year            INTEGER NOT NULL,
            round           INTEGER NOT NULL,
            name            TEXT    NOT NULL,
            circuit         TEXT,
            country         TEXT,
            race_date       TEXT,
            created_at      TEXT    DEFAULT (datetime('now')),
            UNIQUE (year, round)
        )
    """)
    print("  ✓ races table ready")

    # ── Results ───────────────────────────────────────────────────────────────
    # One row per driver per race — the granular race-by-race data.
    # This is the most detailed table and links everything together.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS results (
            result_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id         INTEGER NOT NULL,
            driver_id       INTEGER NOT NULL,
            team_id         INTEGER NOT NULL,
            finish_position INTEGER,
            grid_position   INTEGER,
            points          REAL    DEFAULT 0,
            laps_completed  INTEGER,
            status          TEXT,
            fastest_lap     INTEGER DEFAULT 0,
            created_at      TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (race_id)   REFERENCES races(race_id),
            FOREIGN KEY (driver_id) REFERENCES drivers(driver_id),
            FOREIGN KEY (team_id)   REFERENCES teams(team_id),
            UNIQUE (race_id, driver_id)
        )
    """)
    print("  ✓ results table ready")

    # ── Race Weather ──────────────────────────────────────────────────────────
    # One row per race with averaged conditions during the race window.
    # rainfall = 1 means it rained (precipitation_mm > 0.5 or OpenF1 flagged it)
    # source tells us where the data came from: 'openf1' or 'open-meteo'
    #
    # LEARNING NOTE: We store temperatures as REAL (floating point) because
    # weather data comes back as decimals. INTEGER would truncate 23.7 to 23.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS race_weather (
            weather_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            race_id          INTEGER UNIQUE NOT NULL,
            temp_air         REAL,
            temp_track       REAL,
            humidity         REAL,
            wind_speed       REAL,
            rainfall         INTEGER DEFAULT 0,
            precipitation_mm REAL    DEFAULT 0.0,
            source           TEXT,
            created_at       TEXT    DEFAULT (datetime('now')),
            FOREIGN KEY (race_id) REFERENCES races(race_id)
        )
    """)
    print("  ✓ race_weather table ready")

    # Commit saves all the CREATE TABLE statements to the database file
    # LEARNING NOTE: SQLite uses transactions — changes aren't written
    # to disk until you call commit(). If something crashes before commit,
    # none of the changes are saved (keeps the DB clean).
    conn.commit()


# ── Verify Setup ───────────────────────────────────────────────────────────────

def verify_tables(conn):
    """
    Query SQLite's internal schema table to confirm all tables exist.

    LEARNING NOTE: sqlite_master is a special built-in table that SQLite
    uses to track everything in the database — tables, indexes, etc.
    """

    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    print(f"\n  Database contains {len(tables)} table(s): {', '.join(tables)}")
    return tables


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"F1 Research Database — Setup")
    print(f"DB path: {DB_PATH}\n")

    # Make sure the /root/f1-data/ directory exists
    # exist_ok=True means no error if the folder already exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    try:
        # connect() creates the .db file if it doesn't exist yet
        conn = sqlite3.connect(DB_PATH)
        print("Creating tables...")

        create_tables(conn)
        verify_tables(conn)

        conn.close()
        print("\nSetup complete! Ready for Phase 2 (fetch_data.py).")

    except Exception as e:
        print(f"\nERROR: {e}")


if __name__ == "__main__":
    main()
