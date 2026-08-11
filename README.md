# F1 Research Database

A personal SQLite-backed research project for exploring Formula 1 history — team performance, driver lineups, and race weather — pulled from public APIs and queried through a small interactive CLI.

Built as a learning project: each script is commented with `LEARNING NOTE`s explaining the *why* behind SQL and Python patterns used, not just the *what*.

## Data sources

| Source | Used for |
|---|---|
| [Jolpica F1](https://api.jolpi.ca/ergast/f1) (Ergast-compatible) | Teams, drivers, races, results, season standings (2015–present) |
| [OpenF1](https://openf1.org/) | Current driver/team lineups, race weather (2023+) |
| [Open-Meteo](https://open-meteo.com/) | Historical race weather (2015–2022) and next-race forecast |

All API calls are cached to disk (`cache/`) so re-running a script doesn't re-hit rate limits.

## Project structure

| Script | Purpose |
|---|---|
| `db_setup.py` | Creates the SQLite database and table schema. Run once. |
| `fetch_data.py` | Fetches teams, drivers, races, results, and standings. Supports `--refresh-current` to only refresh the current season. |
| `fetch_weather.py` | Fetches race weather (historical + forecast). Supports `--forecast-only`. |
| `repair_drivers.py` | One-time repair pass: resets driver `active` flags and re-derives them from the most recent season each driver appears in. |
| `explore.py` | Interactive menu for browsing the data — team profiles, comparisons, race schedule, and a "next race" predictor. Supports `--lock-next-race` to lock in that prediction non-interactively (for cron). |
| `train_model.py` | Fits the prediction model's weights from historical results (see [Prediction model](#prediction-model) below). Runs automatically at the end of `fetch_data.py`; can also be run standalone. |

## Setup

```bash
pip install -r requirements.txt
python db_setup.py
python fetch_data.py        # first run takes ~45-60 min due to API rate limits
python fetch_weather.py
python repair_drivers.py
```

Re-runs of `fetch_data.py` and `fetch_weather.py` are fast — cached API responses are reused, and both scripts use `INSERT OR IGNORE` so they're safe to run repeatedly.

## Usage

```bash
python explore.py
```

Menu options: search for a team profile, view all team profiles, compare teams side by side, view the race schedule, and get a prediction for the next race.

To pull fresh data for the current season only (e.g. after a new race weekend):

```bash
python fetch_data.py --refresh-current
python fetch_weather.py --forecast-only
```

### Predictions: preview vs. locked-in

`predict_next_race` (menu option 5) can be run at any time and always shows a
**live, unofficial preview** recalculated from current form — right up until
the prediction is locked in.

Predictions lock in automatically on **race morning**, once the weather
forecast is as accurate as it's going to get, via:

```bash
python explore.py --lock-next-race
```

This is non-interactive and safe to run repeatedly (it's a no-op if it's not
race day yet, or if today's prediction is already locked). It's meant to run
once, automatically, race morning — see [Automation](#automation-cron) below.
Once locked, `predict_next_race` and the race schedule (menu option 4) show
that same official prediction alongside the actual result once the race is
run.

## Prediction model

`predict_next_race` scores each active driver from 4 form components — recent
races, same-circuit history, last season's standing, and the driver's team's
recent form (car/pace strength independent of that driver specifically) —
plus a wet-weather boost when rain is forecast.

The weights combining those components aren't hand-picked guesses anymore.
`train_model.py` rebuilds, for every historical race, exactly the features
the model would have seen *before* that race happened (no lookahead), then
fits a linear regression against what actually happened. The fitted weights
are saved to the `model_weights` table and read by `explore.py` at prediction
time — falling back to reasonable defaults only if there isn't yet enough
history to train on (fewer than 50 results).

This retrains automatically at the end of every `fetch_data.py` run (full or
`--refresh-current`), so the model keeps learning as each new race adds data.
You can also trigger it manually:

```bash
python train_model.py
```

## Automation (cron)

None of these scripts need to run as a long-lived service — they're short
scripts that do one pass and exit, so a daily cron job is enough to keep the
database current. Example crontab (adjust the hour so the weather/lock job
runs comfortably before lights out on a race day, in the server's local time):

```cron
# Refresh current-season results + forecast every morning
0 6 * * * cd /root/f1-data && python fetch_data.py --refresh-current >> refresh.log 2>&1
5 6 * * * cd /root/f1-data && python fetch_weather.py --forecast-only >> refresh.log 2>&1

# Lock in today's prediction — only fires on an actual race morning
10 6 * * * cd /root/f1-data && python explore.py --lock-next-race >> refresh.log 2>&1
```

Since `race_date` only stores a date (no start time), pick a cron hour early
enough to beat every race's local start time with margin.

## Database schema

SQLite database (`f1_data.db`, gitignored — regenerate it with the scripts above):

- **teams** — one row per constructor
- **drivers** — one row per driver, linked to their current/most recent team
- **seasons** — one row per driver per year (points, wins, final standing)
- **races** — one row per Grand Prix event
- **results** — one row per driver per race (the most granular table)
- **race_weather** — one row per race (averaged conditions, source-tagged `openf1` / `open-meteo` / `open-meteo-forecast`)
- **predictions** — one row per driver per race, locked in on race morning (see [Predictions](#predictions-preview-vs-locked-in) above); never overwritten once saved
- **model_weights** — single row holding the learned prediction weights (see [Prediction model](#prediction-model) above); replaced each time `train_model.py` runs

## Notes

- Paths are resolved relative to each script's own location, so this runs the same on any machine.
- No personal or private data is stored — everything comes from public F1 APIs.
