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
| `explore.py` | Interactive menu for browsing the data — team profiles, comparisons, race schedule, and a simple "next race" predictor. |

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

Menu options: search for a team profile, view all team profiles, compare teams side by side, view the race schedule, and get a simple prediction for the next race.

To pull fresh data for the current season only (e.g. after a new race weekend):

```bash
python fetch_data.py --refresh-current
python fetch_weather.py --forecast-only
```

## Database schema

SQLite database (`f1_data.db`, gitignored — regenerate it with the scripts above):

- **teams** — one row per constructor
- **drivers** — one row per driver, linked to their current/most recent team
- **seasons** — one row per driver per year (points, wins, final standing)
- **races** — one row per Grand Prix event
- **results** — one row per driver per race (the most granular table)
- **race_weather** — one row per race (averaged conditions, source-tagged `openf1` / `open-meteo` / `open-meteo-forecast`)

## Notes

- Paths are resolved relative to each script's own location, so this runs the same on any machine.
- No personal or private data is stored — everything comes from public F1 APIs.
