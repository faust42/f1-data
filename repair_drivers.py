"""
repair_drivers.py
Author: jacivell

One-time repair script to:
  1. Reset the active flag on ALL drivers to 0
  2. Set team_id and active=1 on drivers from the most recent season
     they appear in (using the seasons table we already have)

Run once after fetch_data.py completes.

Usage:
    python repair_drivers.py
"""

import sqlite3
from datetime import datetime

DB_PATH = "/root/f1-data/f1_data.db"


def main():
    print("Driver Repair Script")
    print(f"DB path : {DB_PATH}")
    print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ── Step 1: Reset everyone to inactive ───────────────────────────────────
    # LEARNING NOTE: We reset all drivers first so we start from a clean
    # slate. This prevents old/retired drivers from staying marked active
    # just because a previous script run set them incorrectly.
    cursor.execute("UPDATE drivers SET active = 0, team_id = NULL")
    print(f"  Reset {cursor.rowcount} drivers to inactive.")

    # ── Step 2: For each driver, find their most recent season ───────────────
    # We use a subquery to get the MAX year each driver appeared in seasons,
    # then join back to get their team_id for that year.
    #
    # LEARNING NOTE: This pattern — SELECT the row matching MAX(year) —
    # is very common in SQL analytics. We can't just do MAX(year) and
    # team_id in the same SELECT without a subquery, because SQL doesn't
    # know which team_id corresponds to the max year without the join.
    cursor.execute("""
        SELECT s.driver_id, s.team_id, s.year
        FROM seasons s
        INNER JOIN (
            SELECT driver_id, MAX(year) AS latest_year
            FROM seasons
            GROUP BY driver_id
        ) latest ON s.driver_id = latest.driver_id
                 AND s.year     = latest.latest_year
    """)
    most_recent_seasons = cursor.fetchall()

    # ── Step 3: Determine the cutoff year for "active" ───────────────────────
    # A driver is considered active if their most recent season is within
    # 2 years of today. This handles mid-career breaks and recent retirees.
    current_year = datetime.now().year
    active_cutoff = current_year - 2   # 2024 or later = active

    updated    = 0
    set_active = 0

    for row in most_recent_seasons:
        driver_id = row["driver_id"]
        team_id   = row["team_id"]
        year      = row["year"]

        # Mark active only if they raced recently enough
        is_active = 1 if year >= active_cutoff else 0

        cursor.execute("""
            UPDATE drivers
            SET team_id = ?,
                active  = ?
            WHERE driver_id = ?
        """, (team_id, is_active, driver_id))

        updated += 1
        if is_active:
            set_active += 1

    conn.commit()
    print(f"  Updated team_id for {updated} drivers.")
    print(f"  Marked {set_active} drivers as active (raced in {active_cutoff} or later).")

    # ── Step 4: Verify — show current active drivers with their teams ─────────
    print("\n  Active drivers by team:")
    print(f"  {'─' * 50}")

    active_drivers = cursor.execute("""
        SELECT
            t.name                              AS team,
            d.forename || ' ' || d.surname      AS driver,
            d.driver_number,
            s_max.year                          AS last_season
        FROM drivers d
        JOIN teams t ON d.team_id = t.team_id
        JOIN (
            SELECT driver_id, MAX(year) AS year
            FROM seasons
            GROUP BY driver_id
        ) s_max ON d.driver_id = s_max.driver_id
        WHERE d.active = 1
        ORDER BY t.name, d.surname
    """).fetchall()

    current_team = None
    for row in active_drivers:
        if row["team"] != current_team:
            print(f"\n  {row['team']}")
            current_team = row["team"]
        num = f"#{row['driver_number']}" if row["driver_number"] else "   "
        print(f"    {num:<5} {row['driver']:<25} (last: {row['last_season']})")

    conn.close()
    print(f"\nRepair complete! Run explore.py to verify team profiles.")


if __name__ == "__main__":
    main()
