#!/usr/bin/env python3
"""Watch Badminton Aréna Skalka (Bizzy e-rezervace) and book free evening courts.

Rules (all overridable via env vars, see CONFIG below):
  * slot starts at or after EARLIEST_START (17:30) and no later than LATEST_START (20:00)
  * slot starts at least MIN_LEAD_HOURS (27h = 24h free-cancel window + 3h buffer) from now
  * slot starts at most MAX_DAYS (14) days from now
  * only on DAYS (Mon-Fri)
  * DURATION_MIN (60) minutes on one court
  * at most one booking per day (days where you already have a reservation are skipped)
  * a day is never booked again once you've had a reservation there - so cancelling sticks.
    The site deletes cancelled reservations, so the bot remembers such days itself (STATE_FILE).

Env: BASKALKA_USERNAME, BASKALKA_PASSWORD, optional DRY_RUN=1, NTFY_TOPIC,
SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/EMAIL_FROM/EMAIL_TO for an email per booking run.
Exits 1 when a booking or the email failed, so GitHub's failed-run email reaches you.
On GitHub Actions (public logs) only totals are logged; dates, courts and errors go to the email.
"""
import hashlib
import hmac
import html as htmllib
import json
import os
import re
import smtplib
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

BASE = "https://baskalka.e-rezervace.cz"
SCHEDULE_URL = f"{BASE}/Branch/pages/Schedule.faces"
LOGIN_URL = f"{BASE}/Branch/pages/WebLogin.faces"
TZ = ZoneInfo("Europe/Prague")
SLOT_MIN = 30
BADMINTON_SERVICE_ID = "40027"


def _env_time(name, default):
    h, m = os.environ.get(name, default).split(":")
    return time(int(h), int(m))


WEEKDAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _env_days(name, default):
    return {WEEKDAY_NAMES.index(d.strip().lower()[:3]) for d in os.environ.get(name, default).split(",")}


CONFIG = {
    "earliest_start": _env_time("EARLIEST_START", "17:30"),
    "latest_start": _env_time("LATEST_START", "20:00"),
    "min_lead": timedelta(hours=float(os.environ.get("MIN_LEAD_HOURS", "27"))),
    "max_days": int(os.environ.get("MAX_DAYS", "14")),
    "days": _env_days("DAYS", "Mon,Tue,Wed,Thu,Fri"),  # date.weekday() numbers
    "duration_min": int(os.environ.get("DURATION_MIN", "60")),
    "dry_run": os.environ.get("DRY_RUN", "0") == "1",
    "ntfy_topic": os.environ.get("NTFY_TOPIC", ""),
    "max_bookings": int(os.environ.get("MAX_BOOKINGS", "0")),  # per run, 0 = no limit
    # The repo is public, so Actions logs are too: keep dates/courts out of them there.
    "private_logs": os.environ.get("PRIVATE_LOGS", "1" if os.environ.get("GITHUB_ACTIONS") == "true" else "0") == "1",
}


def log(*args):
    print(datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S"), *args, flush=True)


def detail(*args):
    """Log something that reveals when you play (dates, courts) - local runs only."""
    if not CONFIG["private_logs"]:
        log(*args)


def ntfy(title, message):
    if not CONFIG["ntfy_topic"]:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{CONFIG['ntfy_topic']}",
            data=message.encode(),
            headers={"Title": title.encode("utf-8"), "Tags": "badminton"},
            timeout=10,
        )
    except requests.RequestException as e:
        log("ntfy failed:", e)


def send_email(subject, body) -> bool:
    host, to = os.environ.get("SMTP_HOST"), os.environ.get("EMAIL_TO")
    if not (host and to):
        log("email not configured (SMTP_HOST / EMAIL_TO) - failing the run so GitHub emails you instead")
        return False
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    port = int(os.environ.get("SMTP_PORT") or "587")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("EMAIL_FROM") or user
    msg["To"] = to
    msg.set_content(body)
    try:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30) if port == 465 else smtplib.SMTP(host, port, timeout=30)
        with smtp:
            if port != 465:
                smtp.starttls()
            if user:
                smtp.login(user, password)
            smtp.send_message(msg)
    except (smtplib.SMTPException, OSError) as e:
        log("email failed:", e)
        return False
    log(f"email sent to {to}")
    return True


@dataclass
class Day:
    """One day of the badminton grid as the server renders it."""

    day: date
    grid_start: datetime  # start of column 0 (6:30 on weekdays, 8:00 on weekends)
    courts: list  # row labels, e.g. "kurt 01"
    cols: int
    taken: set  # occupied (row, col)
    has_mine: bool  # any reservation of the logged-in user on this day

    def col_start(self, col) -> datetime:
        return self.grid_start + timedelta(minutes=SLOT_MIN * col)


@dataclass
class Slot:
    day: Day
    row: int
    col: int

    @property
    def start(self) -> datetime:
        return self.day.col_start(self.col)

    @property
    def court(self) -> str:
        return self.day.courts[self.row]

    def __str__(self):
        end = self.start + timedelta(minutes=CONFIG["duration_min"])
        return f"{self.start:%a %d.%m.} {self.start:%H:%M}-{end:%H:%M} {self.court}"


def extract_schedule_data(html: str) -> dict:
    marker = "var scheduleData = "
    i = html.find(marker)
    if i < 0:
        raise RuntimeError("scheduleData not found in response")
    obj, _ = json.JSONDecoder().raw_decode(html[i + len(marker):])
    return obj


BLOCKED_COLOR = "#FF0000"  # "Blokovaný termín" - arena blocks (tournaments etc.), also unblinded


def is_mine(ev: dict) -> bool:
    # Other people's reservations are rendered "blinded" (no details, not editable).
    if ev.get("canEdit"):
        return True
    return not ev.get("blinded", True) and ev.get("color") != BLOCKED_COLOR


def parse_day(day: date, html: str) -> Day:
    sd = extract_schedule_data(html)
    grid = sd["grids"][0]
    headers = re.findall(r'class="scheduleTimeHeader[^"]*"[^>]*>(?:<[^>]+>)*\s*(\d{1,2}):(\d{2})', html)
    courts = [htmllib.unescape(c).strip() for c in re.findall(r'class="horizontalRowHeader[^"]*"[^>]*><p>([^<]+)</p>', html)]
    if len(headers) != grid["cols"] or len(courts) != grid["rows"]:
        raise RuntimeError(f"unexpected grid shape on {day}: {len(headers)}x{len(courts)} vs {grid['cols']}x{grid['rows']}")
    h, m = map(int, headers[0])
    taken = set()
    has_mine = False
    for ev in sd["events"]:
        (c0, r0), (c1, r1) = ev["start"], ev["end"]  # [col, row], both ends inclusive
        has_mine |= is_mine(ev)
        for row in range(r0, r1 + 1):
            for col in range(c0, c1 + 1):
                taken.add((row, col))
    return Day(day, datetime.combine(day, time(h, m), TZ), courts, grid["cols"], taken, has_mine)


class State:
    """Days you've had a reservation on (booked by the bot or by you), kept across runs.

    Stored as keyed hashes of the dates, so the file reveals nothing without STATE_KEY.
    """

    KEEP = timedelta(days=30)  # the booking window is 14 days, so older entries are in the past

    def __init__(self, path: str, key: str):
        self.path, self.key = path, key.encode()
        try:
            with open(path) as f:
                self.seen = json.load(f)["seen"]  # token -> unix time first seen
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            self.seen = {}
        self.changed = False

    def _token(self, day: date) -> str:
        return hmac.new(self.key, day.isoformat().encode(), hashlib.sha256).hexdigest()[:20]

    def __contains__(self, day: date) -> bool:
        return self._token(day) in self.seen

    def add(self, day: date):
        if day not in self:
            self.seen[self._token(day)] = int(datetime.now(TZ).timestamp())
            self.changed = True

    def save(self):
        cutoff = (datetime.now(TZ) - self.KEEP).timestamp()
        kept = {t: ts for t, ts in self.seen.items() if ts >= cutoff}
        self.changed |= len(kept) != len(self.seen)
        if self.changed:
            with open(self.path, "w") as f:
                json.dump({"seen": kept}, f)
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a") as f:
                f.write(f"state_changed={'true' if self.changed else 'false'}\n")


def free_slots(d: Day, now: datetime):
    n = CONFIG["duration_min"] // SLOT_MIN
    out = []
    for col in range(d.cols - n + 1):
        start = d.col_start(col)
        if not CONFIG["earliest_start"] <= start.time() <= CONFIG["latest_start"]:
            continue
        if start - now < CONFIG["min_lead"] or start - now > timedelta(days=CONFIG["max_days"]):
            continue
        for row in range(len(d.courts)):
            if all((row, c) not in d.taken for c in range(col, col + n)):
                out.append(Slot(d, row, col))
    return out  # sorted by start time, then court


def serialize_form(form) -> dict:
    """Serialize a <form> like a browser would (minus submit buttons)."""
    data = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.has_attr("disabled"):
            continue
        if el.name == "select":
            opt = el.find("option", selected=True) or el.find("option")
            if opt is not None:
                data[name] = opt.get("value", opt.text)
        elif el.name == "textarea":
            data[name] = el.text
        else:
            typ = el.get("type", "text").lower()
            if typ in ("submit", "button", "image", "reset"):
                continue
            if typ in ("checkbox", "radio") and not el.has_attr("checked"):
                continue
            data[name] = el.get("value", "")
    return data


class Bizzy:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
        )
        self.view_state = None
        self.calendar_param = None
        self.html = None

    def _absorb(self, html: str):
        """Remember ViewState / component ids from a full page or an A4J XML response."""
        self.html = html
        m = re.search(r'name="javax\.faces\.ViewState" id="javax\.faces\.ViewState" value="([^"]+)"', html)
        if m:
            self.view_state = m.group(1)
        m = re.search(r"'onchanged':function\(event\).*?'similarityGroupingId':'(scheduleNavigForm:j_id\d+)'", html, re.S)
        if m:
            self.calendar_param = m.group(1)

    def _post(self, data) -> str:
        r = self.s.post(SCHEDULE_URL, data=data, timeout=30)
        r.raise_for_status()
        self._absorb(r.text)
        return r.text

    def login(self, username: str, password: str):
        r = self.s.get(SCHEDULE_URL, timeout=30)
        r.raise_for_status()
        r = self.s.post(
            LOGIN_URL,
            data={"username": username, "password": password, "forwardUrl": "/pages/Schedule.faces", "courseId": ""},
            timeout=30,
        )
        r.raise_for_status()
        self._absorb(r.text)
        if 'id="username"' in r.text and 'name="password"' in r.text:
            raise RuntimeError("Login failed - check BASKALKA_USERNAME / BASKALKA_PASSWORD")
        log("logged in")

    def load_day(self, day: date) -> Day:
        text = self._post({
            "AJAXREQUEST": "scheduleNavigForm:schedule-navig-region",
            "scheduleNavigForm:schedule_calendarInputDate": f"{day.day}.{day.month}.{day.year}",
            "scheduleNavigForm:schedule_calendarInputCurrentDate": f"{day.month:02d}/{day.year}",
            "scheduleNavigForm:service_filter_menu": BADMINTON_SERVICE_ID,
            "scheduleNavigForm:view_filter_menu": "horizontal_service_day",
            "scheduleNavigForm_SUBMIT": "1",
            "javax.faces.ViewState": self.view_state,
            self.calendar_param: self.calendar_param,
        })
        if not re.search(rf"\b(Po|Út|St|Čt|Pá|So|Ne) {day.day}/{day.month}\b", text):
            raise RuntimeError(f"schedule did not switch to {day}")
        return parse_day(day, text)

    def book(self, slot: Slot) -> bool:
        """Open the reservation dialog for `slot`, sanity-check it and press Uložit."""
        text = self._post({
            "AJAXREQUEST": "cdForm:schedule-main-region",
            "cdForm": "cdForm",
            "autoScroll": "",
            "cdForm:j_idcl": "",
            "cdForm:_link_hidden_": "",
            "cdForm:leftPanel": "closed",
            "javax.faces.ViewState": self.view_state,
            "ajaxSingle": "cdForm:addReservationRequest",
            "cdForm:addReservationRequest": "cdForm:addReservationRequest",
            "newReservationData": json.dumps({"gridId": 0, "modelPosition": [slot.col, slot.row], "position": [500, 300]}),
        })
        soup = BeautifulSoup(text, "html.parser")
        form = soup.find("form", id="reservationForm")
        if form is None or form.find("select", id="reservationForm:reservationTimeFrom") is None:
            raise RuntimeError(f"reservation dialog did not open: {soup.get_text(' ', strip=True)[:300]}")
        data = serialize_form(form)

        # Make sure the server pre-filled exactly the slot we asked for.
        d = slot.start
        want_from = f"{d.day}.{d.month}.{d.year} {d.hour}:{d.minute:02d}:00"
        got_from = data.get("reservationForm:reservationTimeFrom")
        if got_from != want_from:
            raise RuntimeError(f"dialog start {got_from!r} != expected {want_from!r}")
        courts = {
            cb.get("value"): cb.parent.get_text(strip=True)
            for cb in form.find_all("input", attrs={"name": "reservationForm:selectedResources"})
        }
        checked = [cb.get("value") for cb in form.find_all("input", attrs={"name": "reservationForm:selectedResources", "checked": True})]
        if [courts.get(v) for v in checked] != [slot.court]:
            raise RuntimeError(f"dialog courts {[courts.get(v) for v in checked]} != expected {slot.court}")
        durations = [o.get("value") for o in form.find("select", id="reservationForm:reservationTimeTo").find_all("option")]
        if str(CONFIG["duration_min"]) not in durations:
            raise RuntimeError(f"duration {CONFIG['duration_min']} not offered (options: {durations})")
        data["reservationForm:reservationTimeTo"] = str(CONFIG["duration_min"])
        data["reservationForm:reservationAccountPaymentSelect"] = "CASH"

        if CONFIG["dry_run"]:
            detail(f"DRY RUN - would book {slot}")
            return False

        data.update({
            "AJAXREQUEST": "_viewRoot",
            "javax.faces.ViewState": self.view_state,
            "reservationForm:reservationEditFormStoreButton": "reservationForm:reservationEditFormStoreButton",
        })
        resp = self._post(data)
        # The store response carries no reliable success marker, so re-read the grid.
        after = self.load_day(slot.day.day)
        if not all((slot.row, c) in after.taken for c in range(slot.col, slot.col + CONFIG["duration_min"] // SLOT_MIN)):
            raise RuntimeError(
                "court not taken after saving; server said: "
                + BeautifulSoup(resp, "html.parser").get_text(" ", strip=True)[:500]
            )
        return True


def candidate_days(now: datetime):
    d = (now + CONFIG["min_lead"]).date()
    last = (now + timedelta(days=CONFIG["max_days"])).date()
    while d <= last:
        if d.weekday() in CONFIG["days"]:
            yield d
        d += timedelta(days=1)


def main():
    if "--test-email" in sys.argv:
        return 0 if send_email("baskalka-bot test", "Email notifications from baskalka-bot work.") else 1
    now = datetime.now(TZ)
    user, pw = os.environ.get("BASKALKA_USERNAME"), os.environ.get("BASKALKA_PASSWORD")
    if not (user and pw):
        sys.exit("BASKALKA_USERNAME / BASKALKA_PASSWORD not set")
    state = State(os.environ.get("STATE_FILE", "state.json"), os.environ.get("STATE_KEY") or user + pw)
    bz = Bizzy()
    bz.login(user, pw)

    booked, failures, checked, candidates = [], [], 0, 0
    for day in candidate_days(now):
        if CONFIG["max_bookings"] and len(booked) >= CONFIG["max_bookings"]:
            break
        d = bz.load_day(day)
        checked += 1
        if d.has_mine:
            state.add(day)
            detail(f"{day:%a %d.%m.}: you already have a reservation, skipping")
            continue
        if day in state:
            detail(f"{day:%a %d.%m.}: you had a reservation here before (cancelled?), skipping")
            continue
        slots = free_slots(d, now)
        if not slots:
            detail(f"{day:%a %d.%m.}: nothing free")
            continue
        slot = slots[0]
        candidates += 1
        detail(f"{day:%a %d.%m.}: {len(slots)} free, trying {slot}")
        try:
            if bz.book(slot):
                booked.append(slot)
                state.add(day)
                detail(f"booked {slot}")
        except Exception as e:  # keep going with other days
            failures.append(f"{slot}: {e}")
            state.add(day)  # one attempt per day: no retry storm, and it may have gone through anyway
            detail(f"FAILED {slot}: {e}")
            bz.load_day(day)  # resync server-side view state

    state.save()
    ok = notify(booked, failures)
    dry = f", dry run: would book {candidates}" if CONFIG["dry_run"] else ""
    log(f"checked {checked} days, booked {len(booked)}, failed {len(failures)}{dry}")
    return 0 if ok and not failures else 1


def notify(booked, failures) -> bool:
    """Email (and ntfy) the details; the public job summary only gets counts."""
    if not (booked or failures):
        return True
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(f"Booked {len(booked)}, failed {len(failures)} - details sent by email.\n")
    if booked:
        subject = f"Badminton booked: {booked[0]}" if len(booked) == 1 else f"Badminton: {len(booked)} courts booked"
    else:
        subject = f"Badminton: booking failed ({len(failures)})"
    lines = []
    if booked:
        lines += ["Booked:", *[f"- {s}  (cancel free until {cancel_deadline(s)})" for s in booked], ""]
    if failures:
        lines += ["Failed (check Moje rezervace - a failed attempt may still have gone through;"
                  " the bot won't retry these days):",
                  *[f"- {f}" for f in failures], ""]
    lines += ["Payment: at the desk (hotově/kartou).", f"Cancel / view: {SCHEDULE_URL} -> Moje rezervace"]
    body = "\n".join(lines)
    ntfy(subject, body)
    return send_email(subject, body)


def cancel_deadline(slot: Slot) -> str:
    return f"{slot.start - timedelta(hours=24):%a %d.%m. %H:%M}"


if __name__ == "__main__":
    sys.exit(main())
