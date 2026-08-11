"""
train_model.py
Author: jacivell

Phase 4 of the F1 Research Database project.
Fits the prediction model's weights from historical race results instead
of hand-picked guesses, and re-fits them as new races complete.

For every historical race, we rebuild the exact same features
compute_prediction_scores() would have seen *before* that race happened
(no lookahead), then fit a linear regression against what actually
happened (the driver's real points that race). The fitted weights are
saved to the model_weights table, which explore.py reads at prediction
time.

Usage:
    python train_model.py

Also used as a library:
    from train_model import retrain_weights
    retrain_weights(conn)   # called automatically after fetch_data.py --refresh-current
"""

import sqlite3
import os
import numpy as np
from datetime import datetime
from explore import position_to_points

# ── Configuration ─────────────────────────────────────────────────────────────

# Resolved relative to this script's location so it works whether you're
# running on the VS Code server or locally (Windows/macOS/Linux).
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "f1_data.db")

# season_pts needs a prior year of standings, so the earliest trainable
# year is one after the first year we ever fetched data for.
MIN_TRAIN_YEAR = 2016

# Wet-score shrinkage — same constants used in explore.py's get_wet_score().
# LEARNING NOTE: A driver with 1 wet race and a win gets a raw wet_score of
# 0.6, same as a driver with 20 wet races at a 60% win rate — pure noise on
# a tiny sample. Shrinking toward a league-average prior (blending in
# PRIOR_N "imaginary" average races) tames that without a special case.
WET_PRIOR_N     = 8
WET_PRIOR_SCORE = 0.10

# Recency weights for the "last 5 races" component — most recent race
# first. A simple average treats race 1 and race 5 identically even
# though F1 cars change meaningfully mid-season (upgrades); weighting the
# most recent race heaviest reflects that current form matters more.
RECENCY_WEIGHTS = [5, 4, 3, 2, 1]


# ── Feature Extraction (mirrors explore.py's compute_prediction_scores) ──────
# Every query here is filtered to strictly *before* the race being trained
# on, so the model never sees the future.

def recency_weighted_last5(conn, driver_id, year, before_date):
    rows = conn.execute("""
        SELECT res.finish_position
        FROM   results res
        JOIN   races   r ON res.race_id = r.race_id
        WHERE  res.driver_id = ? AND r.year = ? AND r.race_date < ?
        ORDER  BY r.race_date DESC LIMIT 5
    """, (driver_id, year, before_date)).fetchall()

    if not rows:
        return 0.0

    total_w = sum(RECENCY_WEIGHTS[:len(rows)])
    return sum(
        position_to_points(r["finish_position"]) * w
        for r, w in zip(rows, RECENCY_WEIGHTS)
    ) / total_w


def circuit_history(conn, driver_id, circuit, year):
    row = conn.execute("""
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
    """, (driver_id, circuit, year)).fetchone()
    return round(row["avg_pts"], 2) if row and row["avg_pts"] is not None else 0.0


def last_season_standing(conn, driver_id, year):
    row = conn.execute("""
        SELECT final_position FROM seasons WHERE driver_id = ? AND year = ?
    """, (driver_id, year - 1)).fetchone()
    return position_to_points(row["final_position"]) if row else 0


def team_form(conn, team_id, year, before_date):
    """Average points per race (both cars combined) over the team's last 5 races."""
    row = conn.execute("""
        SELECT AVG(pts) AS avg_pts FROM (
            SELECT r.race_id, AVG(res.points) AS pts
            FROM   results res
            JOIN   races   r ON res.race_id = r.race_id
            WHERE  res.team_id = ? AND r.year = ? AND r.race_date < ?
            GROUP  BY r.race_id
            ORDER  BY r.race_date DESC LIMIT 5
        )
    """, (team_id, year, before_date)).fetchone()
    return round(row["avg_pts"], 2) if row and row["avg_pts"] is not None else 0.0


def wet_score_shrunk(conn, driver_id, before_date):
    row = conn.execute("""
        SELECT
            COUNT(*)                                                    AS total,
            SUM(CASE WHEN res.finish_position  = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN res.finish_position <= 3 THEN 1 ELSE 0 END) AS podiums
        FROM   results res
        JOIN   race_weather rw ON res.race_id = rw.race_id
        JOIN   races r ON res.race_id = r.race_id
        WHERE  res.driver_id = ?
          AND  rw.rainfall   = 1
          AND  r.race_date   < ?
    """, (driver_id, before_date)).fetchone()

    total = row["total"] or 0
    if total == 0:
        return WET_PRIOR_SCORE

    win_rate    = (row["wins"]    or 0) / total
    podium_rate = (row["podiums"] or 0) / total
    raw_score   = (win_rate * 0.6) + (podium_rate * 0.4)

    return (raw_score * total + WET_PRIOR_N * WET_PRIOR_SCORE) / (total + WET_PRIOR_N)


# ── Training ──────────────────────────────────────────────────────────────────

def build_training_rows(conn):
    """
    Walk every completed race from MIN_TRAIN_YEAR onward and build one
    training row per driver result: (features, actual_points, is_wet).
    """
    races = conn.execute("""
        SELECT race_id, year, circuit, race_date
        FROM   races
        WHERE  year >= ? AND race_date IS NOT NULL
        ORDER  BY race_date
    """, (MIN_TRAIN_YEAR,)).fetchall()

    rows = []
    for race in races:
        results = conn.execute("""
            SELECT driver_id, team_id, points
            FROM   results
            WHERE  race_id = ? AND points IS NOT NULL
        """, (race["race_id"],)).fetchall()

        is_wet = conn.execute("""
            SELECT rainfall FROM race_weather WHERE race_id = ?
        """, (race["race_id"],)).fetchone()
        is_wet = bool(is_wet and is_wet["rainfall"] == 1)

        for res in results:
            features = {
                "last5":   recency_weighted_last5(conn, res["driver_id"], race["year"], race["race_date"]),
                "circuit": circuit_history(conn, res["driver_id"], race["circuit"], race["year"]),
                "season":  last_season_standing(conn, res["driver_id"], race["year"]),
                "team":    team_form(conn, res["team_id"], race["year"], race["race_date"]),
            }
            wet = wet_score_shrunk(conn, res["driver_id"], race["race_date"]) if is_wet else None
            rows.append((features, res["points"], wet))

    return rows


def fit_linear_weights(rows):
    """OLS fit: actual_points ~ intercept + w_last5*last5 + w_circuit*circuit + w_season*season + w_team*team."""
    X = np.array([[1.0, r[0]["last5"], r[0]["circuit"], r[0]["season"], r[0]["team"]] for r in rows])
    y = np.array([r[1] for r in rows])

    coeffs, *_ = np.linalg.lstsq(X, y, rcond=None)
    return coeffs  # [intercept, w_last5, w_circuit, w_season, w_team]


def fit_wet_boost(rows, coeffs):
    """
    Fit final = base_score * (1 + wet_boost_k * wet_score) for wet races
    only. Rearranged: (actual - base) = wet_boost_k * (base * wet_score),
    a single-slope, no-intercept regression solved in closed form.
    """
    intercept, w_last5, w_circuit, w_season, w_team = coeffs

    xs, ys = [], []
    for features, actual, wet in rows:
        if wet is None:
            continue
        base = (
            intercept
            + w_last5   * features["last5"]
            + w_circuit * features["circuit"]
            + w_season  * features["season"]
            + w_team    * features["team"]
        )
        x = base * wet
        if x == 0:
            continue
        xs.append(x)
        ys.append(actual - base)

    if not xs:
        return 0.40  # fallback to the original hand-picked default

    xs = np.array(xs)
    ys = np.array(ys)
    denom = np.sum(xs * xs)
    if denom == 0:
        return 0.40

    k = float(np.sum(xs * ys) / denom)
    # Clamp to a sane range — an unbounded fit on limited wet-race data
    # could otherwise swing to an implausible boost.
    return max(-0.5, min(1.0, k))


def retrain_weights(conn):
    """
    Fit and save fresh model weights from all historical results so far.
    Safe to call repeatedly (e.g. after every race) — each call replaces
    the single stored weight row with the latest fit.
    """
    # LEARNING NOTE: every query below reads columns by name (row["year"]),
    # which requires sqlite3.Row. We set this defensively rather than
    # trusting the caller, since fetch_data.py's connections don't set it.
    conn.row_factory = sqlite3.Row

    rows = build_training_rows(conn)

    if len(rows) < 50:
        print(f"  Only {len(rows)} training rows available (need 50+) — skipping fit, keeping existing weights.")
        return

    coeffs = fit_linear_weights(rows)
    wet_k  = fit_wet_boost(rows, coeffs)
    intercept, w_last5, w_circuit, w_season, w_team = coeffs

    conn.execute("""
        INSERT INTO model_weights
            (id, intercept, w_last5, w_circuit, w_season, w_team, wet_boost_k, races_trained, trained_at)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(id) DO UPDATE SET
            intercept     = excluded.intercept,
            w_last5       = excluded.w_last5,
            w_circuit     = excluded.w_circuit,
            w_season      = excluded.w_season,
            w_team        = excluded.w_team,
            wet_boost_k   = excluded.wet_boost_k,
            races_trained = excluded.races_trained,
            trained_at    = excluded.trained_at
    """, (float(intercept), float(w_last5), float(w_circuit), float(w_season),
          float(w_team), float(wet_k), len(rows)))
    conn.commit()

    print(f"  ✓ Model retrained on {len(rows)} historical results.")
    print(f"    intercept={intercept:.2f}  last5={w_last5:.3f}  circuit={w_circuit:.3f}  "
          f"season={w_season:.3f}  team={w_team:.3f}  wet_boost_k={wet_k:.3f}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if not os.path.exists(DB_PATH):
        print("ERROR: Database not found. Run db_setup.py first.")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    print("F1 Prediction Model — Training")
    print(f"DB path : {DB_PATH}")
    print(f"Started : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    retrain_weights(conn)

    conn.close()
    print(f"\nDone! Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
