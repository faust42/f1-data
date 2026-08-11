"""
fetch_data.py
Author: jacivell

Phase 2 of the F1 Research Database project.
Fetches data from the Jolpica F1 API (Ergast-compatible) and
the OpenF1 API, with disk caching to respect the 500 req/hour limit.

Cache lives at: /root/f1-data/cache/
On first run, responses are fetched and saved to cache.
On re-runs, cache is used instead of hitting the API again.

Run db_setup.py first before running this script.

Usage:
    python fetch_data.py
"""

import sqlite3
import requests
import time
import os
import json
import argparse
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────

# Resolved relative to this script's location so it works whether you're
# running on the VS Code server or locally (Windows/macOS/Linux).
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE_DIR, "f1_data.db")
CACHE_DIR  = os.path.join(BASE_DIR, "cache")

START_YEAR = 2015
# LEARNING NOTE: datetime.now().year always returns the current calendar year.
# This means END_YEAR updates automatically every January 1st — no manual edits needed.
END_YEAR   = datetime.now().year

JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
OPENF1_BASE  = "https://api.openf1.org/v1"

MAX_RETRIES  = 3
RETRY_DELAY  = 15   # seconds between retries on failure

# Jolpica: 500 req/hour = 1 per 7.2s to be safe we use 8s
# This means the full fetch will take roughly 45-60 minutes on first run.
# After that, everything is cached and re-runs are instant.
API_DELAY    = 8.0  # seconds between live API calls


# ── Cache Helpers ─────────────────────────────────────────────────────────────

def cache_key_from_url(url, params=None):
    """
    Build a filesystem-safe filename from a URL + params.

    LEARNING NOTE: We can't use the URL directly as a filename because
    it contains slashes and special characters. We replace them with
    underscores to get a flat, safe filename like:
      ergast_f1_2023_results__limit=100&offset=0.json
    """
    key = url.replace("https://", "").replace("/", "_").replace(".", "_")
    if params:
        param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        key = f"{key}__{param_str}"
    return os.path.join(CACHE_DIR, f"{key}.json")


def load_from_cache(cache_path):
    """Return parsed JSON from cache file, or None if it doesn't exist."""
    if os.path.exists(cache_path):
        with open(cache_path, "r") as f:
            return json.load(f)
    return None


def save_to_cache(cache_path, data):
    """Save parsed JSON data to a cache file."""
    with open(cache_path, "w") as f:
        json.dump(data, f)


# ── Fetch with Cache + Retry ───────────────────────────────────────────────────

def fetch_with_retry(url, params=None, source="API"):
    """
    Fetch a URL with disk caching and up to MAX_RETRIES attempts.

    LEARNING NOTE: The cache check happens BEFORE any network call.
    If we have a cached response, we return it immediately and never
    touch the API — this is how we stay under the 500 req/hour limit
    on re-runs and partial fetches.

    Flow:
      1. Build cache path from URL + params
      2. If cache file exists → return it (no API call)
      3. If not → call the API, save response to cache, return it
    """

    cache_path = cache_key_from_url(url, params)

    # Cache hit — return immediately, no API call needed
    cached = load_from_cache(cache_path)
    if cached is not None:
        return cached

    # Cache miss — call the API with retry logic
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()

            # Save to cache before returning so future runs skip this call
            save_to_cache(cache_path, data)

            # Polite delay after every live API call
            time.sleep(API_DELAY)

            return data

        except requests.exceptions.RequestException as e:
            print(f"\n    [{source}] Attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                print(f"    Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    Skipping after {MAX_RETRIES} failed attempts.")
                return None


def progress(current, total, label=""):
    """Print an inline progress bar that overwrites itself each update."""
    pct = int((current / total) * 100)
    bar = ("█" * (pct // 5)).ljust(20)
    print(f"\r  [{bar}] {pct:>3}%  {label:<40}", end="", flush=True)


# ── Ergast / Jolpica Fetchers ──────────────────────────────────────────────────

def extract(data, keys, source):
    """
    Safely walk a nested dict by a list of keys, e.g. extract(data,
    ["MRData", "ConstructorTable", "Constructors"], "Jolpica/teams").

    LEARNING NOTE: The Jolpica/Ergast API can return a 200 OK with an
    unexpected JSON shape (rate-limit soft-errors, maintenance pages).
    Without this check, a plain data["MRData"]["X"]["Y"] would raise an
    uncaught KeyError and crash the whole run mid-loop. Returning None
    instead lets the caller treat it like "no data" and move on.
    """
    node = data
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            print(f"\n    [{source}] Unexpected response shape (missing '{key}') — skipping.")
            return None
        node = node[key]
    return node


def fetch_teams(conn):
    """
    Fetch all F1 constructors and insert into the teams table.
    Uses pagination (limit/offset) to get all pages.
    """

    print("\n[1/5] Fetching teams...")
    cursor      = conn.cursor()
    offset      = 0
    limit       = 100
    total_added = 0

    while True:
        url  = f"{JOLPICA_BASE}/constructors.json"
        data = fetch_with_retry(url, params={"limit": limit, "offset": offset}, source="Jolpica/teams")

        if not data:
            break

        constructors = extract(data, ["MRData", "ConstructorTable", "Constructors"], "Jolpica/teams")
        if not constructors:
            break

        for c in constructors:
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO teams
                        (constructor_id, name, nationality)
                    VALUES (?, ?, ?)
                """, (
                    c["constructorId"],
                    c["name"],
                    c.get("nationality", "Unknown")
                ))
                if cursor.rowcount > 0:
                    total_added += 1

            except Exception as e:
                print(f"\n    ERROR inserting team {c.get('name')}: {e}")

        offset += limit

    conn.commit()
    print(f"  ✓ {total_added} new teams added")


def fetch_drivers(conn):
    """
    Fetch all F1 drivers and insert into the drivers table.
    Pagination handles the full all-time driver list.
    """

    print("\n[2/5] Fetching drivers...")
    cursor      = conn.cursor()
    offset      = 0
    limit       = 100
    total_added = 0

    while True:
        url  = f"{JOLPICA_BASE}/drivers.json"
        data = fetch_with_retry(url, params={"limit": limit, "offset": offset}, source="Jolpica/drivers")

        if not data:
            break

        drivers = extract(data, ["MRData", "DriverTable", "Drivers"], "Jolpica/drivers")
        if not drivers:
            break

        for d in drivers:
            try:
                cursor.execute("""
                    INSERT OR IGNORE INTO drivers
                        (ergast_id, forename, surname, nationality, date_of_birth, driver_number)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    d["driverId"],
                    d["givenName"],
                    d["familyName"],
                    d.get("nationality", "Unknown"),
                    d.get("dateOfBirth"),
                    int(d["permanentNumber"]) if d.get("permanentNumber") else None
                ))
                if cursor.rowcount > 0:
                    total_added += 1

            except Exception as e:
                print(f"\n    ERROR inserting driver {d.get('driverId')}: {e}")

        offset += limit

    conn.commit()
    print(f"  ✓ {total_added} new drivers added")


def fetch_races(conn, year):
    """Fetch the race calendar for a given year."""

    cursor = conn.cursor()
    url    = f"{JOLPICA_BASE}/{year}.json"
    data   = fetch_with_retry(url, source=f"Jolpica/races/{year}")

    if not data:
        return 0

    races_data = extract(data, ["MRData", "RaceTable", "Races"], f"Jolpica/races/{year}")
    if races_data is None:
        return 0

    added = 0
    for r in races_data:
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO races
                    (year, round, name, circuit, country, race_date)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                int(r["season"]),
                int(r["round"]),
                r["raceName"],
                r["Circuit"]["circuitName"],
                r["Circuit"]["Location"]["country"],
                r.get("date")
            ))
            if cursor.rowcount > 0:
                added += 1

        except Exception as e:
            print(f"\n    ERROR inserting race {r.get('raceName')} {year}: {e}")

    conn.commit()
    return added


def fetch_results(conn, year):
    """
    Fetch all race results for a given year with pagination.
    A full season (~24 races x 20 drivers) needs multiple pages.
    """

    cursor  = conn.cursor()
    offset  = 0
    limit   = 100
    added   = 0

    while True:
        url  = f"{JOLPICA_BASE}/{year}/results.json"
        data = fetch_with_retry(url, params={"limit": limit, "offset": offset}, source=f"Jolpica/results/{year}")

        if not data:
            break

        races = extract(data, ["MRData", "RaceTable", "Races"], f"Jolpica/results/{year}")
        if not races:
            break

        for race in races:
            cursor.execute(
                "SELECT race_id FROM races WHERE year = ? AND round = ?",
                (int(race["season"]), int(race["round"]))
            )
            race_row = cursor.fetchone()
            if not race_row:
                continue
            race_id = race_row[0]

            for result in race.get("Results", []):
                try:
                    driver_id_row = cursor.execute(
                        "SELECT driver_id FROM drivers WHERE ergast_id = ?",
                        (result["Driver"]["driverId"],)
                    ).fetchone()

                    team_id_row = cursor.execute(
                        "SELECT team_id FROM teams WHERE constructor_id = ?",
                        (result["Constructor"]["constructorId"],)
                    ).fetchone()

                    if not driver_id_row or not team_id_row:
                        continue

                    fastest = 1 if result.get("FastestLap", {}).get("rank") == "1" else 0

                    cursor.execute("""
                        INSERT OR IGNORE INTO results
                            (race_id, driver_id, team_id, finish_position,
                             grid_position, points, laps_completed, status, fastest_lap)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        race_id,
                        driver_id_row[0],
                        team_id_row[0],
                        int(result["position"]) if result.get("position") else None,
                        int(result["grid"])     if result.get("grid")     else None,
                        float(result.get("points", 0)),
                        int(result["laps"])     if result.get("laps")     else None,
                        result.get("status"),
                        fastest
                    ))
                    if cursor.rowcount > 0:
                        added += 1

                except Exception as e:
                    print(f"\n    ERROR inserting result: {e}")

        offset += limit

    conn.commit()
    return added


def fetch_standings(conn, year):
    """Fetch end-of-season driver standings for a given year."""

    cursor = conn.cursor()
    url    = f"{JOLPICA_BASE}/{year}/driverStandings.json"
    data   = fetch_with_retry(url, source=f"Jolpica/standings/{year}")

    if not data:
        return 0

    standings_lists = extract(data, ["MRData", "StandingsTable", "StandingsLists"], f"Jolpica/standings/{year}")
    if not standings_lists:
        return 0

    added = 0
    for entry in standings_lists[0].get("DriverStandings", []):
        try:
            driver_id_row = cursor.execute(
                "SELECT driver_id FROM drivers WHERE ergast_id = ?",
                (entry["Driver"]["driverId"],)
            ).fetchone()

            team_id_row = cursor.execute(
                "SELECT team_id FROM teams WHERE constructor_id = ?",
                (entry["Constructors"][0]["constructorId"],)
            ).fetchone() if entry.get("Constructors") else None

            if not driver_id_row or not team_id_row:
                continue

            cursor.execute("""
                INSERT OR IGNORE INTO seasons
                    (year, driver_id, team_id, points, wins, final_position)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                year,
                driver_id_row[0],
                team_id_row[0],
                float(entry.get("points", 0)),
                int(entry.get("wins", 0)),
                int(entry["position"]) if entry.get("position") else None
            ))
            if cursor.rowcount > 0:
                added += 1

        except Exception as e:
            print(f"\n    ERROR inserting standing for {year}: {e}")

    conn.commit()
    return added


# ── OpenF1 Supplement ─────────────────────────────────────────────────────────

def fetch_openf1_supplement(conn):
    """
    Mark currently active drivers using OpenF1 data for 2023-2025.

    LEARNING NOTE: OpenF1 requires a session_key to look up drivers.
    We can't filter by year directly on the drivers endpoint — instead
    we first fetch sessions for the year to get a valid session_key,
    then use that to pull the driver list for that season.
    We only need one race session per year — all drivers who raced that
    year appear in any race session.
    """

    print("\n[4/5] Supplementing with OpenF1...")
    cursor  = conn.cursor()
    updated = 0

    # Build the last 3 seasons dynamically so this never needs updating
    # e.g. in 2026 this produces [2024, 2025, 2026]
    recent_years = list(range(END_YEAR - 2, END_YEAR + 1))

    for year in recent_years:

        # Step 1: Get race sessions for this year
        # session_type=Race filters out practice and qualifying sessions
        sessions_data = fetch_with_retry(
            f"{OPENF1_BASE}/sessions",
            params={"year": year, "session_type": "Race"},
            source=f"OpenF1/sessions/{year}"
        )

        if not sessions_data or not isinstance(sessions_data, list) or len(sessions_data) == 0:
            print(f"\n    No OpenF1 sessions found for {year}, skipping.")
            continue

        # Grab the session_key from the first race of that year
        # LEARNING NOTE: We only need one valid session_key to get the
        # full driver list — so [0] (the first result) is enough.
        session_key = sessions_data[0].get("session_key")
        if not session_key:
            print(f"\n    Could not get session_key for {year}, skipping.")
            continue

        # Step 2: Fetch drivers for that session
        drivers_data = fetch_with_retry(
            f"{OPENF1_BASE}/drivers",
            params={"session_key": session_key},
            source=f"OpenF1/drivers/{year}"
        )

        if not drivers_data or not isinstance(drivers_data, list):
            print(f"\n    No OpenF1 driver data for {year} (session {session_key}), skipping.")
            continue

        # Step 3: Mark each driver as active in our DB by driver number + surname.
        # LEARNING NOTE: Driver numbers get reused across F1 history (e.g. #1,
        # #33, #44 have all belonged to multiple different drivers over the
        # decades). Matching on number alone can silently mark a retired
        # driver active. Requiring the surname to match too makes a false
        # match effectively impossible.
        for driver in drivers_data:
            number  = driver.get("driver_number")
            surname = driver.get("last_name")
            if not number or not surname:
                continue
            try:
                cursor.execute(
                    "UPDATE drivers SET active = 1 WHERE driver_number = ? AND surname = ? COLLATE NOCASE",
                    (number, surname)
                )
                if cursor.rowcount > 0:
                    updated += 1
            except Exception as e:
                print(f"\n    ERROR updating driver #{number}: {e}")

    conn.commit()
    print(f"  ✓ {updated} drivers marked active from OpenF1")


# ── Main ───────────────────────────────────────────────────────────────────────

def clear_cache_for_year(year):
    """
    Delete all cached JSON files that relate to a specific year.

    LEARNING NOTE: os.listdir() returns filenames only (no path).
    We use os.path.join() to build the full path before deleting.
    We check if the year string appears in the filename — since our
    cache filenames are built from the URL, year-specific calls like
    /f1/2025/results.json will contain "2025" in the cache filename.
    """

    if not os.path.exists(CACHE_DIR):
        return 0

    deleted = 0
    for filename in os.listdir(CACHE_DIR):
        # Match files that contain the year AND are F1 data (not OpenF1)
        if str(year) in filename and filename.endswith(".json"):
            os.remove(os.path.join(CACHE_DIR, filename))
            deleted += 1

    return deleted


def main():
    # ── Argument Parsing ──────────────────────────────────────────────────────
    # LEARNING NOTE: argparse is Python's standard library for handling
    # command-line flags. We define --refresh-current as a boolean flag
    # (store_true means its presence alone sets it to True).
    # This is how `python fetch_data.py --refresh-current` works.
    parser = argparse.ArgumentParser(description="F1 Research Database — Data Fetch")
    parser.add_argument(
        "--refresh-current",
        action="store_true",
        help="Clear cache for the current season year and re-fetch live data."
    )
    args = parser.parse_args()

    # Determine which year is "current" — always the latest year in our range
    current_year = END_YEAR

    print("F1 Research Database — Data Fetch")
    print(f"DB path  : {DB_PATH}")
    print(f"Cache    : {CACHE_DIR}")
    print(f"Started  : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if args.refresh_current:
        # --refresh-current mode: only re-fetch the current season
        print(f"\nMode     : REFRESH {current_year} season only")
        print(f"\nNOTE: Re-runs use cache and complete in seconds for historical years.")
        print(f"      Only {current_year} data will be fetched live.\n")
    else:
        print(f"Years    : {START_YEAR}–{END_YEAR}")
        print(f"\nNOTE: First run fetches live data (~45-60 min due to rate limits).")
        print(f"      Re-runs use cache and complete in seconds.\n")

    if not os.path.exists(DB_PATH):
        print("ERROR: Database not found. Run db_setup.py first.")
        return

    os.makedirs(CACHE_DIR, exist_ok=True)

    # ── Handle --refresh-current ──────────────────────────────────────────────
    if args.refresh_current:
        deleted = clear_cache_for_year(current_year)
        print(f"  Cleared {deleted} cache file(s) for {current_year}.")

        # Also delete stale DB rows for the current year so we get clean inserts
        # LEARNING NOTE: We delete races first, then results (which depend on
        # race_id via foreign key), then seasons. Order matters here —
        # deleting a race while results still reference it would violate
        # the foreign key constraint.
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")

        deleted_results  = conn.execute(
            "DELETE FROM results WHERE race_id IN (SELECT race_id FROM races WHERE year = ?)",
            (current_year,)
        ).rowcount
        deleted_races    = conn.execute("DELETE FROM races    WHERE year = ?", (current_year,)).rowcount
        deleted_seasons  = conn.execute("DELETE FROM seasons  WHERE year = ?", (current_year,)).rowcount
        conn.commit()

        print(f"  Cleared DB rows for {current_year}: "
              f"{deleted_races} races, {deleted_results} results, {deleted_seasons} standings.\n")

        # Only fetch the current year
        print(f"Fetching {current_year} data...")
        fetch_races(conn, current_year)
        fetch_results(conn, current_year)
        fetch_standings(conn, current_year)
        fetch_openf1_supplement(conn)

    else:
        # ── Full fetch mode ───────────────────────────────────────────────────
        cached_count = len([f for f in os.listdir(CACHE_DIR) if f.endswith(".json")])
        print(f"  Cache contains {cached_count} existing file(s) — those calls will be skipped.\n")

        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA foreign_keys = ON")

        fetch_teams(conn)
        fetch_drivers(conn)

        years       = list(range(START_YEAR, END_YEAR + 1))
        total_years = len(years)

        print(f"\n[3/5] Fetching per-year data ({START_YEAR}–{END_YEAR})...")

        total_races     = 0
        total_results   = 0
        total_standings = 0

        for i, year in enumerate(years, start=1):
            progress(i, total_years, label=f"Year {year}")
            total_races     += fetch_races(conn, year)
            total_results   += fetch_results(conn, year)
            total_standings += fetch_standings(conn, year)

        print()
        print(f"  ✓ {total_races} races | {total_results} results | {total_standings} standings added")

        fetch_openf1_supplement(conn)

    # ── Retrain prediction model ──────────────────────────────────────────────
    # New results just landed, so re-fit the prediction weights against the
    # full history including them (see train_model.py — this is how the
    # predictor "learns" after every race instead of running on stale guesses).
    print("\n[5/5] Retraining prediction model...")
    import train_model
    train_model.retrain_weights(conn)

    # ── Final summary (both modes) ────────────────────────────────────────────
    print("\nFinal row counts:")
    for table in ["teams", "drivers", "races", "results", "seasons"]:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:<12} {count:>6} rows")

    conn.close()
    print(f"\nFetch complete! Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Ready for Phase 3 (explore.py).")


if __name__ == "__main__":
    main()
