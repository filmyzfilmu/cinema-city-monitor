import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote


# ============================================================
# CONFIGURATION
# ============================================================

CINEMA_ID = "1052"

# How far into the future should we look?
HORIZON_DAYS = 250

# Only notify for screenings in an auditorium containing this text.
AUDITORIUM_PATTERN = "imax"

# Optional movie filter.
# Leave empty to monitor ALL IMAX screenings.
FILM_PATTERN = ""

# Cinema City API
API_BASE = (
    "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
)

# State file
STATE_FILE = Path("state/seen.json")

# Notify!
NOTIFY_DEVICE_ID = os.environ.get("NOTIFY_DEVICE_ID")
NOTIFY_DEVICE_TOKEN = os.environ.get("NOTIFY_DEVICE_TOKEN")


# ============================================================
# HTTP HELPERS
# ============================================================

def get_json(url):
    print(f"GET {url}")

    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CinemaCityMonitor/1.0"
        },
    )

    with urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


# ============================================================
# CINEMA CITY
# ============================================================

def get_available_dates():
    """
    Ask Cinema City which dates have screenings for this cinema.
    """

    today = datetime.now().date()

    horizon = today.toordinal() + HORIZON_DAYS
    horizon_date = datetime.fromordinal(horizon)

    url = (
        f"{API_BASE}/dates/in-cinema/"
        f"{CINEMA_ID}/until/{horizon_date:%Y-%m-%d}"
        f"?attr=&lang=cs_CZ"
    )

    data = get_json(url)

    return extract_dates(data)


def extract_dates(data):
    """
    Cinema City API has changed wrappers over time.
    Try the known structures rather than assuming one exact shape.
    """

    if isinstance(data, list):
        values = data

    elif isinstance(data, dict):

        body = data.get("body", data)

        if isinstance(body, list):
            values = body

        elif isinstance(body, dict):

            for key in ("dates", "date", "items", "results"):
                if isinstance(body.get(key), list):
                    values = body[key]
                    break
            else:
                values = []

        else:
            values = []

    else:
        values = []

    dates = []

    for value in values:

        if isinstance(value, str):
            dates.append(value[:10])

        elif isinstance(value, dict):

            for key in (
                "date",
                "eventDate",
                "eventDateTime",
                "dateTime",
            ):
                candidate = value.get(key)

                if candidate:
                    dates.append(str(candidate)[:10])
                    break

    return sorted(set(dates))


def get_events_for_date(date):
    """
    Download all Cinema City events for one cinema/date.
    """

    url = (
        f"{API_BASE}/film-events/in-cinema/"
        f"{CINEMA_ID}/at-date/{date}"
        f"?attr=&lang=cs_CZ"
    )

    data = get_json(url)

    if not isinstance(data, dict):
        raise RuntimeError("Unexpected Cinema City API response.")

    body = data.get("body", data)

    if not isinstance(body, dict):
        raise RuntimeError("Cinema City API body is not an object.")

    films = body.get("films", [])
    events = body.get("events", [])

    films_by_id = {
        str(f.get("id")): f
        for f in films
        if isinstance(f, dict) and f.get("id") is not None
    }

    result = []

    for event in events:

        if not isinstance(event, dict):
            continue

        film_id = str(event.get("filmId", ""))

        film = films_by_id.get(film_id, {})

        film_name = film.get("name", film_id)

        auditorium = str(
            event.get("auditorium") or ""
        )

        # IMAX filter
        if AUDITORIUM_PATTERN:
            if AUDITORIUM_PATTERN.lower() not in auditorium.lower():
                continue

        # Optional movie filter
        if FILM_PATTERN:
            if FILM_PATTERN.lower() not in film_name.lower():
                continue

        event_id = event.get("id")

        if not event_id:
            continue

        result.append(
            {
                "id": str(event_id),
                "film": film_name,
                "filmLink": film.get("link"),
                "datetime": event.get("eventDateTime"),
                "auditorium": auditorium,
                "attrs": event.get("attributeIds", []),
                "presentationCode": event.get(
                    "presentationCode"
                ),
                "soldOut": bool(event.get("soldOut")),
            }
        )

    return result


# ============================================================
# STATE
# ============================================================

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
        json.dump(
            sorted(seen),
            f,
            ensure_ascii=False,
            indent=2,
        )

    temp_file.replace(STATE_FILE)


# ============================================================
# NOTIFY!
# ============================================================

def send_notification(event):

    if not NOTIFY_DEVICE_ID:
        raise RuntimeError("NOTIFY_DEVICE_ID is missing.")

    if not NOTIFY_DEVICE_TOKEN:
        raise RuntimeError("NOTIFY_DEVICE_TOKEN is missing.")

    event_id = event["id"]

    presentation = event.get("presentationCode")

    if presentation:
        booking_url = (
            "https://tickets.cinemacity.cz/order/"
            + quote(str(presentation))
        )
    else:
        booking_url = (
            "https://tickets.cinemacity.cz/order/"
            + quote(str(event_id))
        )

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

    extra_text = ""

    if extra:
        extra_text = "\n" + " • ".join(extra)

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

        print(
            "Notify! response:",
            response.status,
            response_body,
        )

        if response.status < 200 or response.status >= 300:
            raise RuntimeError(
                f"Notify! returned HTTP {response.status}"
            )


# ============================================================
# MAIN
# ============================================================

def main():

    print("========================================")
    print("Cinema City IMAX Monitor")
    print("========================================")

    if not NOTIFY_DEVICE_ID or not NOTIFY_DEVICE_TOKEN:
        print("ERROR: Notify! credentials are missing.")
        sys.exit(1)

    seen = load_state()

    print(f"Previously seen events: {len(seen)}")

    dates = get_available_dates()

    print(f"Available dates: {len(dates)}")

    all_events = []

    for date in dates:

        try:
            events = get_events_for_date(date)

            print(
                f"{date}: {len(events)} matching events"
            )

            all_events.extend(events)

        except Exception as exc:

            print(
                f"ERROR while processing {date}: {exc}"
            )

    current_ids = set()

    new_events = []

    for event in all_events:

        event_id = event["id"]

        current_ids.add(event_id)

        if event_id not in seen:
            new_events.append(event)

    print(f"Current matching events: {len(current_ids)}")
    print(f"New events: {len(new_events)}")

    # IMPORTANT:
    # Mark events as seen only AFTER notifications are successfully sent.
    #
    # This means a temporary Notify! failure will cause the event
    # to be retried on the next run rather than silently losing it.

    for event in sorted(
        new_events,
        key=lambda e: (
            e.get("datetime") or "",
            e.get("film") or "",
        ),
    ):

        print(
            "NEW:",
            event["film"],
            event.get("datetime"),
            event.get("auditorium"),
        )

        send_notification(event)

        seen.add(event["id"])

    save_state(seen)

    print("State saved.")


if __name__ == "__main__":
    main()
