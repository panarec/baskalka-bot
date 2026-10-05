# baskalka-bot

Books free badminton courts at [Badminton Aréna Skalka](https://baskalka.e-rezervace.cz) (Bizzy e-rezervace).

Every 30 minutes (GitHub Actions) it logs in, walks the next 14 days and, for each day where you
don't have a reservation yet, books the earliest free 1-hour slot that:

- starts between **17:30 and 20:00**
- starts at least **27 h** from now (24 h free-cancellation window + 3 h for you to decide)
- starts at most **14 days** from now

Payment is set to "hotově/kartou" (pay at the desk). Cancel for free up to 24 h before the start.

## How it works

No browser. The schedule page embeds the grid as JSON (`var scheduleData`); events are
`[column, row]` rectangles with inclusive ends, column 0 = first time header (6:30 on weekdays,
8:00 on weekends), 30 min per column. Booking replays the two A4J/JSF requests the UI makes:
`cdForm:addReservationRequest` (opens the dialog for a cell) and
`reservationForm:reservationEditFormStoreButton` (Uložit). Before saving, the bot checks that the
dialog pre-filled exactly the intended start time and court, and afterwards re-reads the grid to
confirm the cells are taken.

Your own reservations show up as `canEdit: true` / `blinded: false`; red events are arena blocks.

## Setup

GitHub repo secrets:

| Secret | |
|---|---|
| `BASKALKA_USERNAME` | login |
| `BASKALKA_PASSWORD` | password |
| `NTFY_TOPIC` | optional - push notification via [ntfy.sh](https://ntfy.sh) on every booking |

Optional repo variables (Settings → Secrets and variables → Actions → Variables):
`EARLIEST_START`, `LATEST_START`, `MIN_LEAD_HOURS`, `MAX_DAYS`, `DURATION_MIN`.

Run manually: Actions → *Book badminton court* → Run workflow (dry run is on by default).

## Local

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in
set -a; . ./.env; set +a
DRY_RUN=1 .venv/bin/python bot.py     # report only
MAX_BOOKINGS=1 .venv/bin/python bot.py
```
