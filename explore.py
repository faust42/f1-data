"""
explore.py
Author: jacivell

Phase 3 of the F1 Research Database project.
Interactive menu for exploring F1 team and driver data
to help decide which team to root for.

Usage:
    python explore.py
"""

import sqlite3
import os
from tabulate import tabulate
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH      = "/root/f1-data/f1_data.db"

# Current year — used throughout so the script stays accurate automatically
CURRENT_YEAR = datetime.now().year

# Last 3 seasons used for "recent form" queries
RECENT_YEARS = list(range(CURRENT_YEAR - 2, CURRENT_YEAR + 1))

# The 10 current F1 constructors (2026 grid)
# These are the Ergast constructor_id slugs in our DB
CURRENT_TEAMS = [
    "red_bull",
    "mercedes",
    "ferrari",
    "mclaren",
    "aston_martin",
    "alpine",
    "williams",
    "rb",
    "kick_sauber",
    "haas",
]

# Display-friendly names mapped to constructor_id
TEAM_DISPLAY_NAMES = {
    "red_bull":     "Red Bull",
    "mercedes":     "Mercedes",
    "ferrari":      "Ferrari",
    "mclaren":      "McLaren",
    "aston_martin": "Aston Martin",
    "alpine":       "Alpine",
    "williams":     "Williams",
    "rb":           "RB (Racing Bulls)",
    "kick_sauber":  "Kick Sauber",
    "haas":         "Haas",
}


# ── Database ──────────────────────────────────────────────────────────────────

def get_connection():
    """Open a DB connection with foreign keys enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    # row_factory lets us access columns by name (row["wins"])
    # instead of by index (row[0]) — much more readable
    # LEARNING NOTE: sqlite3.Row is a lightweight dict-like object.
    # Without this, every query result is a plain tuple.
    conn.row_factory = sqlite3.Row
    return conn


# ── Query Functions ───────────────────────────────────────────────────────────

def get_team_profile(conn, constructor_id):
    """
    Build a full profile for one team covering:
      - 10-year win/podium/points history
      - Recent form (last 2 seasons)
      - Current driver lineup
      - Fastest lap count (aggression indicator)
      - Constructor championships in DB range

    LEARNING NOTE: We use several separate queries and combine the
    results in Python rather than one giant SQL query. This is easier
    to read, debug, and modify — a classic "clear over clever" tradeoff.
    """

    # ── Verify team exists ────────────────────────────────────────────────────
    team = conn.execute(
        "SELECT * FROM teams WHERE constructor_id = ?",
        (constructor_id,)
    ).fetchone()

    if not team:
        print(f"\n  Team '{constructor_id}' not found in database.")
        return

    team_id   = team["team_id"]
    team_name = TEAM_DISPLAY_NAMES.get(constructor_id, team["name"])

    print(f"\n{'═' * 55}")
    print(f"  {team_name.upper()} — TEAM PROFILE")
    print(f"{'═' * 55}")
    print(f"  Nationality : {team['nationality'] or 'Unknown'}")
    print(f"  In F1 since : {team['first_season'] or 'Unknown'}")

    # ── 10-Year Season History ────────────────────────────────────────────────
    # Aggregate points/wins/podiums per year for this team across all drivers
    # SUM() adds up both drivers' contributions each season
    history = conn.execute("""
        SELECT
            s.year,
            SUM(s.points)   AS total_points,
            SUM(s.wins)     AS total_wins,
            MIN(s.final_position) AS best_position
        FROM seasons s
        WHERE s.team_id = ?
        GROUP BY s.year
        ORDER BY s.year
    """, (team_id,)).fetchall()

    if history:
        print(f"\n  10-Year Season History")
        print(f"  {'─' * 45}")
        rows = [
            [
                r["year"],
                f"P{r['best_position']}" if r["best_position"] else "N/A",
                int(r["total_wins"]   or 0),
                int(r["total_points"] or 0),
            ]
            for r in history
        ]
        print(tabulate(
            rows,
            headers=["Year", "Position", "Wins", "Points"],
            tablefmt="simple",
            colalign=("left", "center", "center", "right")
        ))

    # ── Recent Form (2023-2025) ───────────────────────────────────────────────
    # LEARNING NOTE: We use a CASE statement inside AVG() to calculate
    # the percentage of races where the team scored a podium finish (top 3).
    # CASE WHEN condition THEN 1 ELSE 0 END turns a boolean into 1/0,
    # and AVG() of 1s and 0s gives us the percentage directly.
    recent = conn.execute("""
        SELECT
            r.year,
            COUNT(DISTINCT r.race_id)                              AS races,
            SUM(CASE WHEN res.finish_position  = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN res.finish_position <= 3 THEN 1 ELSE 0 END) AS podiums,
            SUM(CASE WHEN res.fastest_lap      = 1 THEN 1 ELSE 0 END) AS fastest_laps,
            ROUND(SUM(res.points), 1)                              AS points
        FROM results res
        JOIN races r ON res.race_id = r.race_id
        WHERE res.team_id = ?
          AND r.year IN (?, ?, ?)
        GROUP BY r.year
        ORDER BY r.year
    """, (team_id, CURRENT_YEAR - 2, CURRENT_YEAR - 1, CURRENT_YEAR)).fetchall()

    if recent:
        print(f"\n  Recent Form ({CURRENT_YEAR - 2}–{CURRENT_YEAR})")
        print(f"  {'─' * 45}")
        rows = [
            [
                r["year"],
                r["races"],
                int(r["wins"]         or 0),
                int(r["podiums"]      or 0),
                int(r["fastest_laps"] or 0),
                r["points"]
            ]
            for r in recent
        ]
        print(tabulate(
            rows,
            headers=["Year", "Races", "Wins", "Podiums", "Fast Laps", "Points"],
            tablefmt="simple",
            colalign=("left", "center", "center", "center", "center", "right")
        ))

    # ── Current Driver Lineup ─────────────────────────────────────────────────
    # Drivers linked to this team who are marked active
    drivers = conn.execute("""
        SELECT
            d.forename || ' ' || d.surname AS driver,
            d.nationality,
            d.driver_number
        FROM drivers d
        WHERE d.team_id = ?
          AND d.active  = 1
        ORDER BY d.surname
    """, (team_id,)).fetchall()

    print(f"\n  Current Driver Lineup")
    print(f"  {'─' * 45}")
    if drivers:
        for d in drivers:
            num = f"#{d['driver_number']}" if d["driver_number"] else "  "
            print(f"  {num:<5} {d['driver']:<25} {d['nationality'] or ''}")
    else:
        print("  No active drivers linked (run --refresh-current to update)")

    # ── Fastest Lap Count (10 years) ──────────────────────────────────────────
    fastest_total = conn.execute("""
        SELECT COUNT(*) AS total
        FROM results res
        JOIN races r ON res.race_id = r.race_id
        WHERE res.team_id    = ?
          AND res.fastest_lap = 1
    """, (team_id,)).fetchone()["total"]

    # ── Championships (wins in seasons table where final_position = 1) ────────
    # LEARNING NOTE: The Ergast data doesn't have a separate championships
    # column populated, so we infer it: if a team had a driver finish
    # P1 in the standings AND won the most races that year, they likely
    # won the constructors title. This is an approximation — we flag it.
    champ_years = conn.execute("""
        SELECT year
        FROM seasons
        WHERE team_id      = ?
          AND final_position = 1
        ORDER BY year
    """, (team_id,)).fetchall()

    print(f"\n  Quick Stats ({CURRENT_YEAR - 10}–{CURRENT_YEAR})")
    print(f"  {'─' * 45}")
    print(f"  Fastest laps set    : {fastest_total}")

    if champ_years:
        years_str = ", ".join(str(r["year"]) for r in champ_years)
        print(f"  Driver champ years  : {years_str}")
    else:
        print(f"  Driver champ years  : None in this range")

    print(f"{'═' * 55}\n")


def get_all_profiles(conn):
    """Print a profile for every current F1 team."""
    print("\nGenerating profiles for all 10 current teams...")
    for constructor_id in CURRENT_TEAMS:
        get_team_profile(conn, constructor_id)
        input("  Press Enter for next team...")


def compare_teams(conn):
    """
    Side-by-side comparison table for teams the user selects.

    LEARNING NOTE: We build a list of dicts and pass it to tabulate()
    which formats it cleanly without us managing spacing manually.
    """

    print("\n  Available teams:")
    for i, cid in enumerate(CURRENT_TEAMS, start=1):
        print(f"    {i:>2}. {TEAM_DISPLAY_NAMES[cid]}")

    raw = input("\n  Enter team numbers to compare (e.g. 1,3,5): ").strip()

    # Parse and validate the user's input
    # LEARNING NOTE: We use a list comprehension with int() conversion,
    # wrapped in try/except to handle non-numeric input gracefully.
    try:
        indices = [int(x.strip()) - 1 for x in raw.split(",")]
    except ValueError:
        print("  Invalid input — please enter numbers separated by commas.")
        return

    selected = []
    for i in indices:
        if 0 <= i < len(CURRENT_TEAMS):
            selected.append(CURRENT_TEAMS[i])
        else:
            print(f"  Skipping invalid number: {i + 1}")

    if not selected:
        print("  No valid teams selected.")
        return

    # Build one summary row per selected team
    rows = []
    for cid in selected:
        team = conn.execute(
            "SELECT team_id FROM teams WHERE constructor_id = ?", (cid,)
        ).fetchone()

        if not team:
            continue
        team_id = team["team_id"]

        # Aggregate stats across all years in range
        stats = conn.execute("""
            SELECT
                SUM(s.wins)                    AS total_wins,
                MIN(s.final_position)          AS best_finish,
                COUNT(CASE WHEN s.final_position = 1 THEN 1 END) AS champ_years
            FROM seasons s
            WHERE s.team_id = ?
        """, (team_id,)).fetchone()

        # Recent form: last 2 full seasons
        recent_start = CURRENT_YEAR - 2
        recent_end   = CURRENT_YEAR - 1
        recent = conn.execute("""
            SELECT
                SUM(CASE WHEN res.finish_position  = 1 THEN 1 ELSE 0 END) AS wins,
                SUM(CASE WHEN res.finish_position <= 3 THEN 1 ELSE 0 END) AS podiums,
                ROUND(SUM(res.points), 0)                                  AS points
            FROM results res
            JOIN races r ON res.race_id = r.race_id
            WHERE res.team_id = ?
              AND r.year BETWEEN ? AND ?
        """, (team_id, recent_start, recent_end)).fetchone()

        fastest = conn.execute("""
            SELECT COUNT(*) AS total
            FROM results
            WHERE team_id = ? AND fastest_lap = 1
        """, (team_id,)).fetchone()["total"]

        rows.append([
            TEAM_DISPLAY_NAMES.get(cid, cid),
            int(stats["total_wins"]   or 0),
            int(stats["champ_years"]  or 0),
            f"P{stats['best_finish']}" if stats["best_finish"] else "N/A",
            int(recent["wins"]        or 0),
            int(recent["podiums"]     or 0),
            int(recent["points"]      or 0),
            fastest,
        ])

    print(f"\n{'═' * 75}")
    print(f"  TEAM COMPARISON (10yr stats | {CURRENT_YEAR - 2}–{CURRENT_YEAR - 1} recent form)")
    print(f"{'═' * 75}")
    print(tabulate(
        rows,
        headers=[
            "Team", "10yr Wins", "Champ Yrs", "Best Pos",
            "Recent Wins", "Recent Pods", "Recent Pts", "Fast Laps"
        ],
        tablefmt="simple",
        colalign=("left","center","center","center","center","center","right","center")
    ))
    print()


def search_team(conn):
    """Look up a single team by name and show its full profile."""
    name = input("\n  Enter team name (partial ok, e.g. 'ferrari', 'red'): ").strip().lower()

    # Search both constructor_id and name columns
    match = conn.execute("""
        SELECT constructor_id
        FROM teams
        WHERE LOWER(constructor_id) LIKE ?
           OR LOWER(name)           LIKE ?
        LIMIT 1
    """, (f"%{name}%", f"%{name}%")).fetchone()

    if not match:
        print(f"  No team found matching '{name}'. Try the full slug (e.g. 'red_bull').")
        return

    get_team_profile(conn, match["constructor_id"])


# ── Menu ───────────────────────────────────────────────────────────────────────

def get_podium(conn, race_id):
    """
    Return the top 3 finishers for a given race_id as a list of strings.
    Includes driver surname and team name for each position.
    """
    podium = conn.execute("""
        SELECT
            res.finish_position,
            d.surname,
            t.name AS team
        FROM   results res
        JOIN   drivers d ON res.driver_id = d.driver_id
        JOIN   teams   t ON res.team_id   = t.team_id
        WHERE  res.race_id          = ?
          AND  res.finish_position <= 3
        ORDER  BY res.finish_position
    """, (race_id,)).fetchall()

    if not podium:
        return []

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    return [
        f"{medals[p['finish_position']]} {p['surname']:<18} {p['team']}"
        for p in podium
    ]


def show_race_schedule(conn):
    """
    Display the current season race calendar.
    Completed races show the top 3 finishers with team names.
    The next upcoming race is flagged with days remaining.
    """

    today = datetime.now().date()

    races = conn.execute("""
        SELECT race_id, round, name, country, race_date
        FROM   races
        WHERE  year = ?
        ORDER  BY round
    """, (CURRENT_YEAR,)).fetchall()

    if not races:
        print(f"\n  No races found for {CURRENT_YEAR}.")
        print("  Try running: python fetch_data.py --refresh-current")
        return

    print(f"\n{chr(9552) * 58}")
    print(f"  {CURRENT_YEAR} F1 SEASON CALENDAR")
    print(f"{chr(9552) * 58}")

    next_race_round = next(
        (
            r["round"] for r in races
            if r["race_date"] and
            datetime.strptime(r["race_date"], "%Y-%m-%d").date() >= today
        ),
        None
    )

    for r in races:
        if r["race_date"]:
            race_date = datetime.strptime(r["race_date"], "%Y-%m-%d").date()
            date_str  = race_date.strftime("%b %d")
        else:
            race_date = None
            date_str  = "TBC"

        if race_date is None:
            status = "TBC"
        elif r["round"] == next_race_round:
            days_away = (race_date - today).days
            status    = f"Up Next ({days_away}d)"
        elif race_date < today:
            status = "Done"
        else:
            status = "Upcoming"

        print(f"\n  R{r['round']:02}  {date_str}  {r['name']} | {r['country']}  [{status}]")

        if status == "Done":
            podium_lines = get_podium(conn, r["race_id"])
            if podium_lines:
                for line in podium_lines:
                    print(f"       {line}")
            else:
                print("       No result data yet.")

    print()


# ── F1 Points Scale ───────────────────────────────────────────────────────────
# Official F1 points for finishing positions 1–10.
# Positions 11+ score 0. We use this to convert finish positions into
# a comparable numeric value for our prediction scoring.
F1_POINTS = {1: 25, 2: 18, 3: 15, 4: 12, 5: 10,
             6: 8,  7: 6,  8: 4,  9: 2,  10: 1}


def position_to_points(position):
    """
    Convert a finishing position to F1 points.

    LEARNING NOTE: dict.get(key, default) returns the value for the key
    if it exists, or the default if it doesn't. So position 15 returns 0
    without us needing an if/else for every position.
    """
    if position is None:
        return 0
    return F1_POINTS.get(int(position), 0)


def get_wet_score(conn, driver_id):
    """
    Calculate a driver's wet race performance score.

    We define a wet race as any race where race_weather.rainfall = 1.
    Wet score = (win_rate x 0.6) + (podium_rate x 0.4)

    LEARNING NOTE: Rates (0.0-1.0) are better than raw counts here
    because drivers race different numbers of wet races in their careers.
    A driver with 3 wins from 5 wet races (0.60) is more impressive
    than one with 5 wins from 30 wet races (0.17).
    """
    row = conn.execute("""
        SELECT
            COUNT(*)                                                    AS total,
            SUM(CASE WHEN res.finish_position  = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN res.finish_position <= 3 THEN 1 ELSE 0 END) AS podiums
        FROM   results res
        JOIN   race_weather rw ON res.race_id = rw.race_id
        WHERE  res.driver_id = ?
          AND  rw.rainfall   = 1
    """, (driver_id,)).fetchone()

    total   = row["total"]   or 0
    wins    = row["wins"]    or 0
    podiums = row["podiums"] or 0

    if total == 0:
        return 0.0, 0, 0, 0

    win_rate    = wins    / total
    podium_rate = podiums / total
    wet_score   = (win_rate * 0.6) + (podium_rate * 0.4)
    return round(wet_score, 3), total, wins, podiums


def predict_next_race(conn):
    """
    Predict podium and constructor outlook for the next race.

    Base score = weighted average of 3 form components:
      Last 3 races              50%
      Same circuit last year    30%
      Last season standing      20%

    Weather boost (when rain forecast):
      final = base x (1 + wet_score x WET_BOOST_MAX)
      WET_BOOST_MAX = 0.40  -> elite wet driver gets up to +40%

    LEARNING NOTE: Multiplying by (1 + boost) keeps the adjustment
    proportional to base score. A strong driver gets a larger absolute
    boost, which reflects reality — pace advantage compounds in the wet.
    """

    WET_BOOST_MAX = 0.40
    today         = datetime.now().date()

    # ── Next race ─────────────────────────────────────────────────────────────
    next_race = conn.execute("""
        SELECT race_id, round, name, circuit, country, race_date
        FROM   races
        WHERE  year      = ?
          AND  race_date >= ?
        ORDER  BY round
        LIMIT  1
    """, (CURRENT_YEAR, today.isoformat())).fetchone()

    if not next_race:
        print("\n  No upcoming races found for this season.")
        return

    race_id   = next_race["race_id"]
    circuit   = next_race["circuit"]
    race_date = next_race["race_date"]
    days_away = (datetime.strptime(race_date, "%Y-%m-%d").date() - today).days

    # ── Forecast weather ──────────────────────────────────────────────────────
    forecast = conn.execute("""
        SELECT temp_air, humidity, wind_speed, rainfall, precipitation_mm
        FROM   race_weather
        WHERE  race_id = ?
    """, (race_id,)).fetchone()

    rain_forecast = forecast["rainfall"] == 1 if forecast else False

    # ── Header ────────────────────────────────────────────────────────────────
    sep = chr(9552) * 62
    print(f"\n{sep}")
    print(f"  NEXT RACE PREDICTION")
    print(f"  {next_race['name']} — {next_race['country']}")
    print(f"  {circuit}")
    print(f"  {race_date}  ({days_away} days away)")

    if forecast:
        cond = "🌧  WET RACE FORECAST" if rain_forecast else "☀  DRY RACE FORECAST"
        print(f"  {cond}  |  Temp: {forecast['temp_air']}°C  "
              f"Humidity: {forecast['humidity']}%  "
              f"Wind: {forecast['wind_speed']} km/h  "
              f"Rain: {forecast['precipitation_mm']}mm")
        if rain_forecast:
            print(f"  💧 Wet boost active — up to +{int(WET_BOOST_MAX*100)}% for top wet drivers")
    else:
        print(f"  ⚠  No forecast — run: python fetch_weather.py --forecast-only")
    print(f"{sep}")

    # ── Active drivers ────────────────────────────────────────────────────────
    drivers = conn.execute("""
        SELECT d.driver_id, d.forename || ' ' || d.surname AS name,
               d.surname, t.name AS team, t.team_id, t.constructor_id
        FROM   drivers d
        JOIN   teams   t ON d.team_id = t.team_id
        WHERE  d.active = 1
          AND  t.constructor_id IN ({})
        ORDER  BY d.surname
    """.format(",".join("?" * len(CURRENT_TEAMS))), CURRENT_TEAMS).fetchall()

    if not drivers:
        print("\n  No active drivers found. Run repair_drivers.py first.")
        return

    scores = []
    for d in drivers:
        driver_id = d["driver_id"]

        # Component 1: last 5 races this season (65%)
        # LEARNING NOTE: We expanded from 3 to 5 races to capture more
        # current-season form. Early in the season (fewer than 5 races done)
        # it uses whatever results exist — the average still works correctly.
        last5 = conn.execute("""
            SELECT res.finish_position
            FROM   results res
            JOIN   races   r ON res.race_id = r.race_id
            WHERE  res.driver_id = ? AND r.year = ?
            ORDER  BY r.race_date DESC LIMIT 5
        """, (driver_id, CURRENT_YEAR)).fetchall()
        last5_pts = (
            sum(position_to_points(r["finish_position"]) for r in last5) / len(last5)
            if last5 else 0
        )

        # ── Slump penalty ─────────────────────────────────────────────────────
        # If a driver has scored 0 points in ALL of their last 5 races,
        # something is clearly wrong with their car or form this season.
        # We apply a 40% penalty to their base score to reflect that reality.
        #
        # LEARNING NOTE: all() returns True if every element in the iterable
        # satisfies the condition. If all last 5 finish positions score 0,
        # the driver is in a genuine slump regardless of past glory.
        all_zero = (
            len(last5) >= 3 and
            all(position_to_points(r["finish_position"]) == 0 for r in last5)
        )
        slump_factor = 0.60 if all_zero else 1.0

        # Component 2: same circuit — avg of last 3 appearances (25%)
        # LEARNING NOTE: We do the position→points conversion inside the
        # inner subquery using CASE WHEN, then AVG() the results in the
        # outer query. SQLite can't call our Python position_to_points()
        # function, so we replicate the F1 points scale directly in SQL.
        # If a driver has fewer than 3 appearances, AVG() still works —
        # it divides by however many rows exist, not always 3.
        cly = conn.execute("""
            SELECT AVG(pts) AS avg_pts
            FROM (
                SELECT
                    CASE res.finish_position
                        WHEN 1  THEN 25 WHEN 2  THEN 18 WHEN 3  THEN 15
                        WHEN 4  THEN 12 WHEN 5  THEN 10 WHEN 6  THEN 8
                        WHEN 7  THEN 6  WHEN 8  THEN 4  WHEN 9  THEN 2
                        WHEN 10 THEN 1  ELSE 0
                    END AS pts
                FROM   results res
                JOIN   races r ON res.race_id = r.race_id
                WHERE  res.driver_id = ?
                  AND  r.circuit     = ?
                  AND  r.year        < ?
                ORDER  BY r.race_date DESC
                LIMIT  3
            )
        """, (driver_id, circuit, CURRENT_YEAR)).fetchone()

        circuit_pts = round(cly["avg_pts"], 2) if cly and cly["avg_pts"] is not None else 0

        # Component 3: last season standing (10%)
        # Reduced from 20% — historical pedigree is least predictive
        # for current performance, as Norris's 2026 slump demonstrates
        ls = conn.execute("""
            SELECT final_position FROM seasons
            WHERE  driver_id = ? AND year = ?
        """, (driver_id, CURRENT_YEAR - 1)).fetchone()
        season_pts = position_to_points(ls["final_position"]) if ls else 0

        # Base score with rebalanced weights (must sum to 1.0)
        # 0.65 + 0.25 + 0.10 = 1.0
        base_score = (
            (last5_pts   * 0.65) +
            (circuit_pts * 0.25) +
            (season_pts  * 0.10)
        ) * slump_factor

        # Wet boost
        wet_score, wet_total, wet_wins, wet_pods = get_wet_score(conn, driver_id)
        boost       = wet_score * WET_BOOST_MAX if rain_forecast else 0.0
        final_score = base_score * (1 + boost)

        scores.append({
            "driver":      d["name"],
            "surname":     d["surname"],
            "team":        d["team"],
            "constructor": d["constructor_id"],
            "score":       round(final_score,  2),
            "base_score":  round(base_score,   2),
            "last5_pts":   round(last5_pts,    1),
            "circuit_pts": circuit_pts,
            "season_pts":  season_pts,
            "slump":       all_zero,
            "wet_score":   wet_score,
            "wet_total":   wet_total,
            "wet_wins":    wet_wins,
            "wet_pods":    wet_pods,
            "boost":       round(boost, 3),
        })

    scores.sort(key=lambda x: x["score"], reverse=True)
    medals      = {1: "🥇", 2: "🥈", 3: "🥉"}
    team_medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    dash        = chr(9472)

    # ── Driver prediction ─────────────────────────────────────────────────────
    print(f"\n  DRIVER PODIUM PREDICTION  (top 10)")
    print(f"  {dash * 68}")

    if rain_forecast:
        print(f"  {'':4} {'Driver':<22} {'Team':<20} {'Score':>6}  {'Base':>6}  {'Boost':>5}  Wet Record")
        print(f"  {dash * 68}")
        for i, s in enumerate(scores[:10], start=1):
            medal     = medals.get(i, f"   {i}.")
            wet_rec   = (f"{s['wet_wins']}W/{s['wet_pods']}P/{s['wet_total']} races"
                         if s["wet_total"] else "no wet data")
            boost_str = f"+{s['boost']*100:.0f}%" if s["boost"] > 0 else "  —"
            print(f"  {medal}  {s['driver']:<22} {s['team']:<20} "
                  f"{s['score']:>6.2f}  {s['base_score']:>6.2f}  {boost_str:>5}  {wet_rec}")
    else:
        print(f"  {'':4} {'Driver':<22} {'Team':<20} {'Score':>6}  Breakdown")
        print(f"  {dash * 68}")
        for i, s in enumerate(scores[:10], start=1):
            medal = medals.get(i, f"   {i}.")
            slump = " ⚠slump" if s["slump"] else ""
            bd    = f"(L5:{s['last5_pts']:>4} | Cct:{s['circuit_pts']:>2} | Sea:{s['season_pts']:>2}){slump}"
            print(f"  {medal}  {s['driver']:<22} {s['team']:<20} {s['score']:>6.2f}  {bd}")

    # ── Constructor outlook ───────────────────────────────────────────────────
    team_scores = {}
    for s in scores:
        team_scores.setdefault(s["constructor"], {"team": s["team"], "score": 0.0, "drivers": []})
        team_scores[s["constructor"]]["score"]   += s["score"]
        team_scores[s["constructor"]]["drivers"].append(s["surname"])

    sorted_teams = sorted(team_scores.values(), key=lambda x: x["score"], reverse=True)

    print(f"\n  CONSTRUCTOR OUTLOOK")
    print(f"  {dash * 48}")
    for i, t in enumerate(sorted_teams, start=1):
        medal   = team_medals.get(i, f"   {i}.")
        drivers = " & ".join(t["drivers"])
        print(f"  {medal}  {t['team']:<22} {t['score']:>6.2f}  ({drivers})")

    # ── Key ───────────────────────────────────────────────────────────────────
    print(f"\n  Key: L5=last 5 races (x0.65) | Cct=circuit last yr (x0.25) | Sea=last season (x0.10)")
    print(f"  ⚠slump = 0pts in last 3+ races → 40% penalty applied to base score")
    if rain_forecast:
        print(f"  Wet boost: base x (1 + wet_score x {WET_BOOST_MAX}) | max +{int(WET_BOOST_MAX*100)}%")
    print(f"  ⚠  Form model — excludes strategy, mechanical failures, luck.\n")



def print_menu():
    print("\n╔══════════════════════════════════════╗")
    print("║       F1 Team Research Tool          ║")
    print("╠══════════════════════════════════════╣")
    print("║  1. Profile — single team            ║")
    print("║  2. Profile — all current teams      ║")
    print("║  3. Compare selected teams           ║")
    print("║  4. Race schedule — current season   ║")
    print("║  5. Predict next race                ║")
    print("║  6. Exit                             ║")
    print("╚══════════════════════════════════════╝")


def main():
    if not os.path.exists(DB_PATH):
        print("ERROR: Database not found. Run db_setup.py and fetch_data.py first.")
        return

    conn = get_connection()

    print("\nWelcome to the F1 Research Tool!")
    print("Use this to explore team data and find your team to root for.\n")

    while True:
        print_menu()
        choice = input("  Choose an option (1-6): ").strip()

        if choice == "1":
            search_team(conn)

        elif choice == "2":
            get_all_profiles(conn)

        elif choice == "3":
            compare_teams(conn)

        elif choice == "4":
            show_race_schedule(conn)

        elif choice == "5":
            predict_next_race(conn)

        elif choice == "6":
            print("\n  Exiting. Enjoy the racing!\n")
            break

        else:
            print("  Invalid choice — please enter 1 through 6.")

    conn.close()


if __name__ == "__main__":
    main()
