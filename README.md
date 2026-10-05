# baskalka-bot

Books free badminton courts at [Badminton Aréna Skalka](https://baskalka.e-rezervace.cz) (Bizzy e-rezervace).

Every 5 minutes (GitHub Actions) it logs in, walks the next 14 days and, for each day where you
don't have a reservation yet, books the earliest free 1-hour slot that:

- starts between **17:30 and 20:00**
- starts at least **27 h** from now (24 h free-cancellation window + 3 h for you to decide)
- starts at most **14 days** from now
- is on a **weekday** (Mon-Fri)

Payment is set to "hotově/kartou" (pay at the desk). Cancel for free up to 24 h before the start.

After a run that booked something you get one email listing the courts and their free-cancellation
deadlines. If a booking or that email fails, the run is marked failed, so GitHub's own
"workflow failed" email reaches you instead.

The repo is public, so its Actions logs are too. There the bot logs only totals
("checked 10 days, booked 1, failed 0"); dates, courts and error details go only into the email.
Local runs log everything (force either way with `PRIVATE_LOGS=1` / `0`).

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
| `EMAIL_TO` | where booking emails go |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | SMTP account that sends them (port 465 = SSL, otherwise STARTTLS) |
| `EMAIL_FROM` | optional, defaults to `SMTP_USER` |
| `NTFY_TOPIC` | optional - push notification via [ntfy.sh](https://ntfy.sh) on every booking |

SMTP options that work:

- **Resend** (free): sign up with the `EMAIL_TO` address, create an API key, then
  `SMTP_HOST=smtp.resend.com`, `SMTP_PORT=465`, `SMTP_USER=resend`, `SMTP_PASSWORD=<api key>`,
  `EMAIL_FROM=onboarding@resend.dev` (without a verified domain Resend only delivers to your own address - which is the point).
- **Gmail** with an [app password](https://myaccount.google.com/apppasswords):
  `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=<you>@gmail.com`, `SMTP_PASSWORD=<app password>`.

Optional repo variables (Settings → Secrets and variables → Actions → Variables):
`EARLIEST_START`, `LATEST_START`, `MIN_LEAD_HOURS`, `MAX_DAYS`, `DURATION_MIN`, `DAYS` (e.g. `Mon,Wed,Fri`).

Run manually: Actions → *Book badminton court* → Run workflow (dry run is on by default;
tick "Only send a test email" to check the SMTP setup).

## Local

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in
set -a; . ./.env; set +a
DRY_RUN=1 .venv/bin/python bot.py     # report only
MAX_BOOKINGS=1 .venv/bin/python bot.py
```
