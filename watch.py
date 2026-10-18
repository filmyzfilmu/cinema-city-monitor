"""
Cinema City IMAX Monitor
========================

Watches Cinema City Praha Flora for new IMAX / 70mm screenings and sends a
push notification to the Notify! iOS app for each new one.

Besides looking for screenings, every run also checks that Cinema City's
data still looks the way this script expects. If anything is off (the
website can't be reached, the data format changed, the IMAX hall was
renamed, notifications can't be sent...), the run is marked as FAILED.
Failed runs don't send the heartbeat ping to healthchecks.io, so if the
problem lasts, healthchecks.io emails you.
"""

import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen


CINEMA_ID = "1052"
HORIZON_DAYS = 250

# --- IMAX detection -----------------------------------------------------
# Checked against Cinema City's live data on 18 Oct 2026: IMAX screenings
# at Praha Flora take place in the auditorium "IMAX VOLVO", and real 70mm
# prints carry the "70-mm" attribute. A screening counts as a match if
# EITHER signal is present (OR, not AND), so if Cinema City changes one of
# them, the other still catches it.
AUDITORIUM_PATTERN = "imax"       # case-insensitive substring of auditorium name; "" disables this check
ATTRIBUTE_PATTERNS = ["70-mm"]    # case-insensitive attributeIds to treat as a match; [] disables this check

FILM_PATTERN = ""  # case-insensitive substring of film title; "" matches all films

# Fields on each screening that this script relies on. If Cinema City
# renames or removes one of them, the data health check fails the run.
REQUIRED_EVENT_FIELDS = [
    "id",
    "filmId",
    "eventDateTime",
    "auditorium",
    "attributeIds",
]

API_BASE = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
STATE_FILE = Path("state/seen.json")

# Each request to Cinema City is tried up to 3 times, so a single hiccup
# doesn't fail the whole run.
REQUEST_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = [3, 10]

NOTIFY_DEVICE_ID = os.environ.get("NOTIFY_DEVICE_ID")
NOTIFY_DEVICE_TOKEN = os.environ.get("NOTIFY_DEVICE_TOKEN")


# ------------------------------------------------------------------------
# Talking to Cinema City
# ------------------------------------------------------------------------

def get_json(url):
    last_error = None

    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        suffix = f" (attempt {attempt})" if attempt > 1 else ""
        print(f"GET {url}{suffix}")

        try:
            request = Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 CinemaCityMonitor/1.0"},
            )

            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        except Exception as exc:
            last_error = exc
            print(f"  request failed: {exc}")

            if attempt < REQUEST_ATTEMPTS:
                time.sleep(RETRY_DELAYS_SECONDS[attempt - 1])

    raise RuntimeError(str(last_error)) from last_error


def get_available_dates():
    horizon_date = date.today() + timedelta(days=HORIZON_DAYS)

    # Note: we deliberately do NOT pass attr=70-mm here even though the API
    # supports server-side attribute filtering. That would silently exclude
    # IMAX screenings that are only identifiable by auditorium name.
    # Fetching everything and filtering locally guarantees nothing is missed.
    url = (
        f"{API_BASE}/dates/in-cinema/"
        f"{CINEMA_ID}/until/{horizon_date:%Y-%m-%d}"
        f"?attr=&lang=cs_CZ"
    )

    return extract_dates(get_json(url))


def extract_dates(data):
    # Expected shape (checked 18 Oct 2026): {"body": {"dates": ["2026-10-18", ...]}}
    # The extra branches tolerate small shape changes. If none of them
    # finds anything, the result is empty and the health check fails the run.
    if isinstance(data, list):
        values = data

    elif isinstance(data, dict):
        body = data.get("body", data)

        if isinstance(body, list):
            values = body

        elif isinstance(body, dict):
            values = []

            for key in ("dates", "date", "items", "results"):
                if isinstance(body.get(key), list):
                    values = body[key]
                    break

        else:
            values = []

    else:
        values = []

    dates = []

    for value in values:
        if isinstance(value, str):
            dates.append(value[:10])

        elif isinstance(value, dict):
            for key in ("date", "eventDate", "eventDateTime", "dateTime"):
                candidate = value.get(key)

                if candidate:
                    dates.append(str(candidate)[:10])
                    break

    return sorted(set(dates))


def matches_target_screening(auditorium, attrs):
    if not AUDITORIUM_PATTERN and not ATTRIBUTE_PATTERNS:
        # Both checks disabled on purpose -- match everything.
        return True

    auditorium_lower = (auditorium or "").lower()
    attrs_lower = {str(a).lower() for a in (attrs or [])}

    auditorium_match = bool(AUDITORIUM_PATTERN) and (
        AUDITORIUM_PATTERN.lower() in auditorium_lower
    )

    attribute_match = bool(ATTRIBUTE_PATTERNS) and any(
        pattern.lower() in attrs_lower for pattern in ATTRIBUTE_PATTERNS
    )

    return auditorium_match or attribute_match


def get_events_for_date(day):
    """Returns (matching_events, stats) for one date.

    stats describes the raw data (all screenings, not just IMAX) so the
    health check can notice when Cinema City changes its data format.
    """
    url = (
        f"{API_BASE}/film-events/in-cinema/"
        f"{CINEMA_ID}/at-date/{day}"
        f"?attr=&lang=cs_CZ"
    )

    data = get_json(url)

    if not isinstance(data, dict):
        raise RuntimeError("unexpected response (not a JSON object)")

    body = data.get("body", data)

    if not isinstance(body, dict):
        raise RuntimeError("unexpected response ('body' is not an object)")

    films = body.get("films") or []
    events = body.get("events") or []

    films_by_id = {
        str(f.get("id")): f
        for f in films
        if isinstance(f, dict) and f.get("id") is not None
    }

    stats = {
        "events": 0,
        "films": len(films_by_id),
        "target_screenings": 0,
        "fields": {field: 0 for field in REQUIRED_EVENT_FIELDS},
    }

    result = []

    for event in events:
        if not isinstance(event, dict):
            continue

        stats["events"] += 1

        for field in REQUIRED_EVENT_FIELDS:
            if field in event:
                stats["fields"][field] += 1

        film_id = str(event.get("filmId", ""))
        film = films_by_id.get(film_id, {})
        film_name = film.get("name", film_id)

        auditorium = str(event.get("auditorium") or "")
        attrs = event.get("attributeIds", []) or []

        if not matches_target_screening(auditorium, attrs):
            continue

        stats["target_screenings"] += 1

        if FILM_PATTERN and FILM_PATTERN.lower() not in film_name.lower():
            continue

        event_id = event.get("id")

        if not event_id:
            continue

        # Prefer the API's own booking link. The fallback uses the same
        # pattern as the real links (checked 18 Oct 2026), e.g.
        # https://www.cinemacity.cz/cz/booking-router/launch/1052/265655?lang=cs
        booking_link = event.get("bookingRouterLaunchLink") or (
            f"https://www.cinemacity.cz/cz/booking-router/launch/"
            f"{CINEMA_ID}/{quote(str(event_id))}?lang=cs"
        )

        result.append({
            "id": str(event_id),
            "film": film_name,
            "filmLink": film.get("link"),
            "datetime": event.get("eventDateTime"),
            "auditorium": auditorium,
            "attrs": attrs,
            "bookingLink": booking_link,
            "soldOut": bool(event.get("soldOut")),
        })

    return result, stats


# ------------------------------------------------------------------------
# Data health check -- catches changes on Cinema City's side
# ------------------------------------------------------------------------

def check_data_health(totals, report_problem):
    """Looks for signs that Cinema City changed its data.

    Only called when at least one date loaded successfully.
    """
    if totals["events"] == 0:
        report_problem(
            "Cinema City returned dates but no screenings at all. "
            "The format of its data has probably changed."
        )
        return

    for field in REQUIRED_EVENT_FIELDS:
        if totals["fields"][field] == 0:
            report_problem(
                f"The field '{field}' is missing from all "
                f"{totals['events']} screenings. Cinema City has probably "
                f"changed the format of its data, so the monitor may miss "
                f"IMAX screenings."
            )

    if totals["films"] == 0:
        report_problem(
            "Cinema City returned screenings but no film list, so "
            "notifications would show film IDs instead of titles."
        )

    if totals["target_screenings"] == 0:
        report_problem(
            f"Found 0 IMAX/70mm screenings among all {totals['events']} "
            f"screenings in the program. Cinema City may have renamed the "
            f"IMAX hall or changed how it marks IMAX screenings (or the "
            f"IMAX hall really has no program)."
        )


# ------------------------------------------------------------------------
# State (which screenings were already notified)
# ------------------------------------------------------------------------

def load_state():
    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(str(x) for x in data)

        if isinstance(data, dict):
            return set(str(x) for x in data.get("seen", []))

    except Exception as exc:
        print(f"WARNING: Cannot read state file: {exc}")

    return set()


def save_state(seen):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temp_file = STATE_FILE.with_suffix(".tmp")

    with temp_file.open("w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=2)

    temp_file.replace(STATE_FILE)


# ------------------------------------------------------------------------
# Notifications
# ------------------------------------------------------------------------

def send_notification(event):
    booking_url = event["bookingLink"]
    attrs = event.get("attrs") or []

    extra = []

    if "70-mm" in attrs:
        extra.append("70mm")

    if "subbed" in attrs:
        extra.append("titulky")

    if "dubbed" in attrs:
        extra.append("dabing")

    if event.get("soldOut"):
        extra.append("VYPRODÁNO")

    extra_text = ("\n" + " • ".join(extra)) if extra else ""

    datetime_text = event.get("datetime") or "neznámý čas"

    text = (
        f"{event['film']}\n"
        f"{datetime_text}\n"
        f"{event['auditorium']}"
        f"{extra_text}\n\n"
        f"🎟️ {booking_url}"
    )

    payload = {
        "title": "Nová projekce Cinema City IMAX",
        "text": text,
        "groupType": "cinema-city-imax",
    }

    url = (
        "https://push.getnotifyapp.com/notify-json/"
        f"{quote(NOTIFY_DEVICE_ID)}"
        f"?token={quote(NOTIFY_DEVICE_TOKEN)}"
    )

    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "CinemaCityMonitor/1.0",
        },
        method="POST",
    )

    with urlopen(request, timeout=30) as response:
        response_body = response.read().decode("utf-8")

        print("Notify! response:", response.status, response_body)

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Notify! returned HTTP {response.status}")


# ------------------------------------------------------------------------
# Run summary (shown on the run's page on GitHub)
# ------------------------------------------------------------------------

def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not path:
        return

    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def finish(problems, totals, new_count):
    summary = [
        "## Cinema City IMAX Monitor",
        "",
        f"- Dates checked: {totals['dates_ok']} of {totals['dates']}",
        f"- Screenings checked: {totals['events']}",
        f"- IMAX/70mm screenings in the program: {totals['target_screenings']}",
        f"- New screenings this run: {new_count}",
        "",
    ]

    if problems:
        summary.append("### ❌ Problems found")
        summary.extend(f"- {p}" for p in problems)
        write_summary(summary)

        print("========================================")
        print(f"RUN FAILED: {len(problems)} problem(s) found:")
        for p in problems:
            print(f"  - {p}")
        print("No heartbeat will be sent for this run.")
        sys.exit(1)

    summary.append("### ✅ All health checks passed")
    write_summary(summary)
    print("All health checks passed.")


# ------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------

def main():
    print("========================================")
    print("Cinema City IMAX Monitor")
    print("========================================")

    if not NOTIFY_DEVICE_ID or not NOTIFY_DEVICE_TOKEN:
        print("::error title=Cinema City monitor::Notify! credentials are missing (check the repository secrets).")
        sys.exit(1)

    problems = []

    def report_problem(message):
        message = " ".join(str(message).split())  # keep it on one line
        print(f"::error title=Cinema City monitor::{message}")
        problems.append(message)

    seen = load_state()
    print(f"Previously seen events: {len(seen)}")

    totals = {
        "dates": 0,
        "dates_ok": 0,
        "events": 0,
        "films": 0,
        "target_screenings": 0,
        "fields": {field: 0 for field in REQUIRED_EVENT_FIELDS},
    }

    try:
        dates = get_available_dates()
    except Exception as exc:
        report_problem(f"Could not load the list of dates from Cinema City: {exc}")
        dates = []
    else:
        if not dates:
            report_problem(
                "Cinema City returned no dates at all. The format of its "
                "data has probably changed."
            )

    totals["dates"] = len(dates)
    print(f"Available dates: {len(dates)}")

    all_events = []
    failed_dates = []

    for day in dates:
        try:
            events, stats = get_events_for_date(day)
        except Exception as exc:
            print(f"ERROR while processing {day}: {exc}")
            failed_dates.append(f"{day} ({exc})")
            continue

        print(f"{day}: {len(events)} matching events")

        all_events.extend(events)

        totals["dates_ok"] += 1
        totals["events"] += stats["events"]
        totals["films"] += stats["films"]
        totals["target_screenings"] += stats["target_screenings"]

        for field, count in stats["fields"].items():
            totals["fields"][field] += count

    if failed_dates:
        report_problem(
            f"Could not load screenings for {len(failed_dates)} of "
            f"{len(dates)} dates, e.g. {failed_dates[0]}"
        )

    if totals["dates_ok"] > 0:
        check_data_health(totals, report_problem)

    current_ids = set()
    new_events = []

    for event in all_events:
        current_ids.add(event["id"])

        if event["id"] not in seen:
            new_events.append(event)

    if os.environ.get("SEED_MODE") == "1":
        print("SEED MODE: registering current events without notifications.")
        seen.update(current_ids)
        save_state(seen)
        print(f"Seeded {len(current_ids)} events.")
        finish(problems, totals, 0)
        return

    print(f"Current matching events: {len(current_ids)}")
    print(f"New events: {len(new_events)}")

    # Notify per-event, but don't let one failed notification take down the
    # whole run. A failed send is not marked as seen, so it is retried on
    # the next run.
    failed_sends = []

    for event in sorted(
        new_events,
        key=lambda e: (e.get("datetime") or "", e.get("film") or ""),
    ):
        print("NEW:", event["film"], event.get("datetime"), event.get("auditorium"))

        try:
            send_notification(event)
        except Exception as exc:
            print(f"ERROR sending notification for {event['id']}: {exc}")
            failed_sends.append(str(exc))
            continue

        seen.add(event["id"])

    save_state(seen)
    print("State saved.")

    if failed_sends:
        report_problem(
            f"Could not send {len(failed_sends)} notification(s) to Notify!. "
            f"They will be retried on the next run. Last error: {failed_sends[-1]}"
        )

    finish(problems, totals, len(new_events) - len(failed_sends))


if __name__ == "__main__":
    main()
