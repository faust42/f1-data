"""
fetch_weather.py
Author: jacivell

Fetches historical and forecast weather for all F1 races in the DB.

Sources:
  - OpenF1  → 2023+ race sessions (air temp, track temp, humidity, wind, rainfall)
  - Open-Meteo → 2015-2022 historical + next race forecast (lat/lon + date based)

Weather is stored in the race_weather table, one row per race.
Safe to re-run — uses INSERT OR IGNORE, existing rows are skipped.

Run after fetch_data.py and db_setup.py.

Usage:
    python fetch_weather.py
    python fetch_weather.py --forecast-only   (just update next race forecast)
"""

import sqlite3
import requests
import time
import os
import json
import argparse
import difflib
import hashlib
from datetime import datetime, timedelta

# ── Configuration ─────────────────────────────────────────────────────────────

# Resolved relative to this script's location so it works whether you're
# running on the VS Code server or locally (Windows/macOS/Linux).
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
DB_PATH   = os.path.join(BASE_DIR, "f1_data.db")
CACHE_DIR = os.path.join(BASE_DIR, "cache")

OPENF1_BASE   = "https://api.openf1.org/v1"
OPENMETEO_BASE = "https://api.open-meteo.com/v1"
OPENMETEO_FORECAST_BASE = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_ARCHIVE_BASE  = "https://archive-api.open-meteo.com/v1/archive"

API_DELAY   = 1.0   # Open-Meteo is generous but let's be polite
MAX_RETRIES = 3
RETRY_DELAY = 10

# Rainfall threshold — anything above this is considered a "wet race"
WET_THRESHOLD_MM = 0.5

# ── Circuit Coordinates ───────────────────────────────────────────────────────
# Open-Meteo requires lat/lon. We match on circuit name from our races table.
# LEARNING NOTE: This is a lookup table — a dict that maps a known key
# (circuit name) to a value (coordinates). Much faster than an API call
# for each circuit, and these coordinates don't change year to year.

CIRCUIT_COORDS = {
    "Albert Park Grand Prix Circuit":       (-37.8497, 144.9680),
    "Shanghai International Circuit":       (31.3389,  121.2198),
    "Suzuka Circuit":                        (34.8431,  136.5407),
    "Bahrain International Circuit":         (26.0325,   50.5106),
    "Jeddah Corniche Circuit":               (21.6319,   39.1044),
    "Miami International Autodrome":         (25.9581,  -80.2389),
    "Circuit Gilles Villeneuve":             (45.5000,  -73.5228),
    "Circuit de Monaco":                     (43.7347,    7.4205),
    "Circuit de Barcelona-Catalunya":        (41.5700,    2.2611),
    "Circuit de Madrid":                     (40.2831,   -3.7276),
    "Red Bull Ring":                         (47.2197,   14.7647),
    "Silverstone Circuit":                   (52.0786,   -1.0169),
    "Circuit de Spa-Francorchamps":          (50.4372,    5.9714),
    "Hungaroring":                           (47.5789,   19.2486),
    "Circuit Park Zandvoort":               (52.3888,    4.5409),
    "Autodromo Nazionale di Monza":          (45.6156,    9.2811),
    "Baku City Circuit":                     (40.3725,   49.8533),
    "Marina Bay Street Circuit":             (1.2914,   103.8642),
    "Circuit of the Americas":               (30.1328,  -97.6411),
    "Autodromo Hermanos Rodriguez":          (19.4042,  -99.0907),
    "Autodromo Jose Carlos Pace":            (-23.7036, -46.6997),
    "Las Vegas Strip Street Circuit":        (36.1147, -115.1728),
    "Losail International Circuit":          (25.4900,   51.4542),
    "Yas Marina Circuit":                    (24.4672,   54.6031),
    # Older circuits still in historical data
    "Hockenheimring":                        (49.3278,    8.5656),
    "Autodromo Enzo e Dino Ferrari":         (44.3439,   11.7167),
    "Istanbul Park":                         (40.9517,   29.4050),
    "Nürburgring":                           (50.3356,    6.9475),
    "Sepang International Circuit":          (2.7606,   101.7381),
    "Buddh International Circuit":           (28.3487,   77.5331),
    "Circuit of the Americas (COTA)":        (30.1328,  -97.6411),
    "Sochi Autodrom":                        (43.4057,   39.9578),
    "Autodromo Internazionale del Mugello":  (43.9975,   11.3719),
    "Portimao":                              (37.2272,   -8.6268),
    "Autodromo Internacional do Algarve":    (37.2272,   -8.6268),
}


def match_circuit_coords(circuit_name):
    """
    Look up lat/lon for a circuit name, falling back to fuzzy matching
    when the name doesn't match CIRCUIT_COORDS exactly.

    LEARNING NOTE: The old fallback did a substring check on the first
    two words of the circuit name (e.g. "de" in "Autodromo Internazionale
    del Mugello"). Short common words match many unrelated keys, so it
    could silently pick the wrong circuit's coordinates. difflib's
    get_close_matches scores overall string similarity instead, which is
    far less likely to false-positive on an unrelated circuit.
    """
    if circuit_name in CIRCUIT_COORDS:
        return CIRCUIT_COORDS[circuit_name]

    close = difflib.get_close_matches(circuit_name, CIRCUIT_COORDS.keys(), n=1, cutoff=0.6)
    return CIRCUIT_COORDS[close[0]] if close else None


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def cache_key(url, params=None):
    """
    Build a filesystem-safe cache filename from URL + params.

    LEARNING NOTE: We used to build this from the literal URL + query
    string, truncated to 180 chars. That's fine on Linux, but on Windows
    the full path (drive + every parent folder + filename) is capped at
    260 characters by default — a long parent folder (e.g. deep inside
    OneDrive) plus a long query string like Open-Meteo's hourly params
    can blow past that limit and fail with a confusing FileNotFoundError.
    Hashing the URL+params gives a short, fixed-length, always-safe
    filename regardless of how long the real request was — and as a
    bonus, avoids two different long URLs colliding after truncation.
    """
    param_str = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items(), key=lambda x: str(x[0])))
    digest    = hashlib.sha1(f"{url}?{param_str}".encode()).hexdigest()[:16]
    slug      = "".join(c if c.isalnum() else "_" for c in url.rstrip("/").split("/")[-1])[:30]
    return os.path.join(CACHE_DIR, f"wx_{slug}_{digest}.json")


def fetch(url, params=None, source="API", use_cache=True):
    """
    Fetch a URL with caching and retry logic.

    LEARNING NOTE: We prefix weather cache files with 'wx_' so they're
    easy to identify separately from the F1 race data cache files.
    This makes it easy to clear just weather cache if needed.
    """
    if use_cache:
        path   = cache_key(url, params)
        cached = None
        if os.path.exists(path):
            with open(path) as f:
                cached = json.load(f)
        if cached is not None:
            return cached

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            if use_cache:
                path = cache_key(url, params)
                with open(path, "w") as f:
                    json.dump(data, f)
            time.sleep(API_DELAY)
            return data
        except requests.exceptions.RequestException as e:
            print(f"\n    [{source}] Attempt {attempt}/{MAX_RETRIES} failed: {e}")
            if attempt < MAX_RETRIES:
                print(f"    Retrying in {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"    Skipping after {MAX_RETRIES} attempts.")
                return None


def progress(current, total, label=""):
    pct = int((current / total) * 100)
    bar = ("█" * (pct // 5)).ljust(20)
    print(f"\r  [{bar}] {pct:>3}%  {label:<40}", end="", flush=True)


# ── OpenF1 Weather (2023+) ────────────────────────────────────────────────────

def fetch_openf1_weather(conn):
    """
    Fetch race session weather from OpenF1 for 2023 onward.

    OpenF1 returns weather readings every ~1 minute during a session.
    We average them to get representative race conditions.

    LEARNING NOTE: We use AVG() in Python here rather than SQL because
    the data comes back as a list of dicts from the API. We sum each
    field across all readings then divide by the count — same as SQL AVG().
    """

    print("\n[1/3] Fetching OpenF1 weather (2023+)...")
    cursor  = conn.cursor()
    added   = 0
    skipped = 0

    current_year = datetime.now().year

    # Get all races from 2023 onward that don't have weather yet
    races = cursor.execute("""
        SELECT r.race_id, r.year, r.round, r.name, r.race_date
        FROM   races r
        LEFT JOIN race_weather rw ON r.race_id = rw.race_id
        WHERE  r.year >= 2023
          AND  rw.race_id IS NULL
          AND  r.race_date IS NOT NULL
          AND  r.race_date <= date('now')
        ORDER  BY r.year, r.round
    """).fetchall()

    if not races:
        print("  ✓ No new OpenF1 races to fetch.")
        return

    total = len(races)

    for i, race in enumerate(races, start=1):
        progress(i, total, label=f"{race['year']} R{race['round']:02} {race['name'][:25]}")

        # Step 1: Find the race session_key for this race
        sessions = fetch(
            f"{OPENF1_BASE}/sessions",
            params={"year": race["year"], "session_type": "Race",
                    "circuit_short_name": None},
            source=f"OpenF1/sessions/{race['year']}/R{race['round']}"
        )

        # Match by finding the session closest to the race date.
        # LEARNING NOTE: The old fallback (when no session fell within 1 day)
        # picked the season's earliest session by sort order — silently
        # attaching the wrong race's weather. Instead we scan every session
        # once and keep whichever has the smallest date difference.
        session_key = None
        if sessions and isinstance(sessions, list):
            race_dt      = datetime.strptime(race["race_date"], "%Y-%m-%d")
            best_session = None
            best_diff    = None
            for s in sessions:
                s_date = s.get("date_start", "")[:10]
                if not s_date:
                    continue
                try:
                    diff = abs((datetime.strptime(s_date, "%Y-%m-%d") - race_dt).days)
                except ValueError:
                    continue
                if best_diff is None or diff < best_diff:
                    best_diff    = diff
                    best_session = s
            if best_session is not None:
                session_key = best_session.get("session_key")

        if not session_key:
            skipped += 1
            continue

        # Step 2: Fetch weather readings for that session
        weather_data = fetch(
            f"{OPENF1_BASE}/weather",
            params={"session_key": session_key},
            source=f"OpenF1/weather/{session_key}"
        )

        if not weather_data or not isinstance(weather_data, list) or len(weather_data) == 0:
            skipped += 1
            continue

        # Step 3: Average all readings across the session
        # LEARNING NOTE: We filter out None values before averaging using
        # a list comprehension with 'if v is not None'. Dividing by 0
        # would crash, so we check len() > 0 before dividing.
        def avg_field(field):
            vals = [r[field] for r in weather_data if r.get(field) is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        temp_air   = avg_field("air_temperature")
        temp_track = avg_field("track_temperature")
        humidity   = avg_field("humidity")
        wind_speed = avg_field("wind_speed")

        # rainfall is a boolean in OpenF1 — True if it rained at any point
        rainfall_readings = [r["rainfall"] for r in weather_data if r.get("rainfall") is not None]
        rainfall = 1 if any(rainfall_readings) else 0

        # Estimate precipitation_mm — OpenF1 doesn't give mm directly
        # We approximate: if rainfall=True for >20% of readings, call it 2mm
        # otherwise 0. This is a rough proxy.
        rain_fraction    = sum(1 for r in rainfall_readings if r) / len(rainfall_readings) if rainfall_readings else 0
        precipitation_mm = round(rain_fraction * 10, 1)  # scale: 100% rain = 10mm

        try:
            cursor.execute("""
                INSERT OR IGNORE INTO race_weather
                    (race_id, temp_air, temp_track, humidity, wind_speed,
                     rainfall, precipitation_mm, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'openf1')
            """, (race["race_id"], temp_air, temp_track, humidity,
                  wind_speed, rainfall, precipitation_mm))

            if cursor.rowcount > 0:
                added += 1

        except Exception as e:
            print(f"\n    ERROR saving weather for race_id {race['race_id']}: {e}")

    conn.commit()
    print()
    print(f"  ✓ {added} races added, {skipped} skipped (no session match)")


# ── Open-Meteo Historical (2015-2022) ─────────────────────────────────────────

def fetch_openmeteo_historical(conn):
    """
    Fetch historical race weather from Open-Meteo for 2015-2022.

    Open-Meteo Archive API works by lat/lon + date range.
    We request hourly data for the race date and average the hours
    that typically cover an F1 race (13:00-17:00 local, roughly 11:00-15:00 UTC).

    LEARNING NOTE: Open-Meteo returns data as parallel arrays:
      { "time": ["2019-03-17T11:00", ...], "temperature_2m": [23.1, ...] }
    This means index 0 of time corresponds to index 0 of temperature_2m.
    We zip them together to pair each timestamp with its values.
    """

    print("\n[2/3] Fetching Open-Meteo historical weather (2015-2022)...")
    cursor  = conn.cursor()
    added   = 0
    skipped = 0

    races = cursor.execute("""
        SELECT r.race_id, r.year, r.round, r.name, r.circuit,
               r.country, r.race_date
        FROM   races r
        LEFT JOIN race_weather rw ON r.race_id = rw.race_id
        WHERE  r.year BETWEEN 2015 AND 2022
          AND  rw.race_id IS NULL
          AND  r.race_date IS NOT NULL
        ORDER  BY r.year, r.round
    """).fetchall()

    if not races:
        print("  ✓ No new historical races to fetch.")
        return

    total = len(races)

    for i, race in enumerate(races, start=1):
        progress(i, total, label=f"{race['year']} R{race['round']:02} {race['name'][:25]}")

        # Look up coordinates for this circuit (exact, then fuzzy match)
        coords = match_circuit_coords(race["circuit"])

        if not coords:
            skipped += 1
            continue

        lat, lon   = coords
        race_date  = race["race_date"]

        # Request hourly data for the race day
        # We ask for a ±1 day window to ensure we get full coverage
        date_start = race_date
        date_end   = race_date

        data = fetch(
            OPENMETEO_ARCHIVE_BASE,
            params={
                "latitude":        lat,
                "longitude":       lon,
                "start_date":      date_start,
                "end_date":        date_end,
                "hourly":          "temperature_2m,relativehumidity_2m,windspeed_10m,precipitation",
                "timezone":        "UTC",
                "windspeed_unit":  "kmh",
            },
            source=f"OpenMeteo/historical/{race['year']}/R{race['round']}"
        )

        if not data or "hourly" not in data:
            skipped += 1
            continue

        hourly = data["hourly"]
        times  = hourly.get("time", [])

        # Pair each timestamp with its weather values
        # LEARNING NOTE: zip() takes multiple iterables and pairs their
        # elements together. zip([1,2,3], ['a','b','c']) → (1,'a'),(2,'b'),(3,'c')
        # We use it here to pair timestamps with each weather variable.
        readings = list(zip(
            times,
            hourly.get("temperature_2m",        [None] * len(times)),
            hourly.get("relativehumidity_2m",    [None] * len(times)),
            hourly.get("windspeed_10m",          [None] * len(times)),
            hourly.get("precipitation",          [None] * len(times)),
        ))

        # Filter to race hours: 11:00-17:00 UTC covers most race start times
        # globally. Not perfect but a reasonable approximation.
        race_readings = [
            r for r in readings
            if r[0] and "T" in r[0] and 11 <= int(r[0].split("T")[1][:2]) <= 17
        ]

        if not race_readings:
            race_readings = readings  # fallback: use all hours that day

        def avg_vals(idx):
            vals = [r[idx] for r in race_readings if r[idx] is not None]
            return round(sum(vals) / len(vals), 2) if vals else None

        temp_air    = avg_vals(1)
        humidity    = avg_vals(2)
        wind_speed  = avg_vals(3)
        precip_vals = [r[4] for r in race_readings if r[4] is not None]
        precip_mm   = round(sum(precip_vals), 2) if precip_vals else 0.0
        rainfall    = 1 if precip_mm > WET_THRESHOLD_MM else 0

        # Open-Meteo archive doesn't have track temp — leave as NULL
        # Track temp is typically ~10-15°C above air temp but varies too
        # much by circuit surface to estimate reliably.
        try:
            cursor.execute("""
                INSERT OR IGNORE INTO race_weather
                    (race_id, temp_air, temp_track, humidity, wind_speed,
                     rainfall, precipitation_mm, source)
                VALUES (?, ?, NULL, ?, ?, ?, ?, 'open-meteo')
            """, (race["race_id"], temp_air, humidity,
                  wind_speed, rainfall, precip_mm))

            if cursor.rowcount > 0:
                added += 1

        except Exception as e:
            print(f"\n    ERROR saving weather for race_id {race['race_id']}: {e}")

    conn.commit()
    print()
    print(f"  ✓ {added} races added, {skipped} skipped (no circuit coords)")


# ── Open-Meteo Forecast (next race) ───────────────────────────────────────────

def fetch_next_race_forecast(conn):
    """
    Fetch the weather forecast for the next upcoming race.

    We DELETE the existing forecast for the next race (if any) and
    re-insert so this stays fresh each time it runs.

    LEARNING NOTE: Unlike historical data which never changes, forecasts
    update daily. So we don't cache forecast calls — we always hit the
    live API and we don't use INSERT OR IGNORE (we want to overwrite).
    Open-Meteo forecasts go out 16 days, which covers the next race
    for most of the season.
    """

    print("\n[3/3] Fetching forecast for next race...")
    cursor = conn.cursor()
    today  = datetime.now().date()

    next_race = cursor.execute("""
        SELECT r.race_id, r.round, r.name, r.circuit, r.country, r.race_date
        FROM   races r
        WHERE  r.year      = ?
          AND  r.race_date >= ?
        ORDER  BY r.round
        LIMIT  1
    """, (datetime.now().year, today.isoformat())).fetchone()

    if not next_race:
        print("  No upcoming race found.")
        return

    print(f"  Next race: {next_race['name']} on {next_race['race_date']}")

    coords = match_circuit_coords(next_race["circuit"])

    if not coords:
        print(f"  No coordinates found for '{next_race['circuit']}' — skipping forecast.")
        return

    lat, lon      = coords
    race_date     = next_race["race_date"]
    race_date_dt  = datetime.strptime(race_date, "%Y-%m-%d").date()
    days_out      = (race_date_dt - today).days

    if days_out > 16:
        print(f"  Race is {days_out} days away — outside 16-day forecast window.")
        print("  Forecast will be available closer to race day.")
        return

    # Fetch forecast — no cache so it's always fresh
    data = fetch(
        OPENMETEO_FORECAST_BASE,
        params={
            "latitude":       lat,
            "longitude":      lon,
            "hourly":         "temperature_2m,relativehumidity_2m,windspeed_10m,precipitation",
            "forecast_days":  16,
            "timezone":       "UTC",
            "windspeed_unit": "kmh",
        },
        source=f"OpenMeteo/forecast/{next_race['name']}",
        use_cache=False   # always fetch live forecast
    )

    if not data or "hourly" not in data:
        print("  Could not fetch forecast data.")
        return

    hourly = data["hourly"]
    times  = hourly.get("time", [])

    readings = list(zip(
        times,
        hourly.get("temperature_2m",     [None] * len(times)),
        hourly.get("relativehumidity_2m",[None] * len(times)),
        hourly.get("windspeed_10m",      [None] * len(times)),
        hourly.get("precipitation",      [None] * len(times)),
    ))

    # Filter to race day hours only
    race_readings = [
        r for r in readings
        if r[0] and race_date in r[0] and "T" in r[0]
        and 11 <= int(r[0].split("T")[1][:2]) <= 17
    ]

    if not race_readings:
        race_readings = [r for r in readings if r[0] and race_date in r[0]]

    if not race_readings:
        print("  No forecast data found for race date.")
        return

    def avg_vals(idx):
        vals = [r[idx] for r in race_readings if r[idx] is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    temp_air   = avg_vals(1)
    humidity   = avg_vals(2)
    wind_speed = avg_vals(3)
    precip_mm  = round(sum(r[4] for r in race_readings if r[4] is not None), 2)
    rainfall   = 1 if precip_mm > WET_THRESHOLD_MM else 0

    # Delete old forecast for this race and replace with fresh data
    # LEARNING NOTE: We explicitly DELETE before inserting because forecasts
    # change daily. INSERT OR REPLACE would also work but DELETE + INSERT
    # makes the intent clearer — we want a fresh row, not a merged one.
    cursor.execute("DELETE FROM race_weather WHERE race_id = ?", (next_race["race_id"],))
    cursor.execute("""
        INSERT INTO race_weather
            (race_id, temp_air, temp_track, humidity, wind_speed,
             rainfall, precipitation_mm, source)
        VALUES (?, ?, NULL, ?, ?, ?, ?, 'open-meteo-forecast')
    """, (next_race["race_id"], temp_air, humidity, wind_speed, rainfall, precip_mm))

    conn.commit()

    condition = "🌧 WET" if rainfall else "☀ DRY"
    print(f"  ✓ Forecast saved: {condition}  "
          f"Temp: {temp_air}°C  "
          f"Humidity: {humidity}%  "
          f"Wind: {wind_speed} km/h  "
          f"Rain: {precip_mm}mm")


# ── Summary ───────────────────────────────────────────────────────────────────

def print_summary(conn):
    """Show how many races now have weather data, broken down by source."""
    print("\n  Weather coverage summary:")
    rows = conn.execute("""
        SELECT source, COUNT(*) as cnt,
               SUM(rainfall) as wet_races
        FROM   race_weather
        GROUP  BY source
        ORDER  BY source
    """).fetchall()

    total_races = conn.execute("SELECT COUNT(*) FROM races WHERE race_date <= date('now')").fetchone()[0]
    total_wx    = conn.execute("SELECT COUNT(*) FROM race_weather").fetchone()[0]

    for r in rows:
        print(f"  {r['source']:<25} {r['cnt']:>4} races  ({r['wet_races']} wet)")

    print(f"\n  Coverage: {total_wx}/{total_races} completed races have weather data")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="F1 Weather Fetcher")
    parser.add_argument(
        "--forecast-only",
        action="store_true",
        help="Only update the next race forecast, skip historical fetch."
    )
    args = parser.parse_args()

    print("F1 Weather Fetch")
    print(f"DB path : {DB_PATH}")
    print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    if not os.path.exists(DB_PATH):
        print("ERROR: Database not found. Run db_setup.py first.")
        return

    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = get_connection()

    if args.forecast_only:
        fetch_next_race_forecast(conn)
    else:
        fetch_openf1_weather(conn)
        fetch_openmeteo_historical(conn)
        fetch_next_race_forecast(conn)

    print_summary(conn)
    conn.close()
    print(f"\nDone! Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("Re-run explore.py — the predictor will now use weather data.")


if __name__ == "__main__":
    main()
