import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote


CINEMA_ID = "1052"
HORIZON_DAYS = 250

# --- IMAX detection -----------------------------------------------------
# Cinema City's API does NOT reliably tag IMAX screenings the same way in
# every field. In practice (confirmed against the Praha Flora / cinema 1052
# API, the same cinema this script targets):
#   - the "imax" attribute EXISTS in the attribute catalogue but is not
#     actually applied to events, so filtering on attributeIds alone for
#     "imax" silently matches nothing.
#   - IMAX screenings instead show up via the auditorium name (e.g.
#     "IMAX VOLVO") and/or the "70-mm" attribute for real 70mm prints.
# To avoid ever missing a screening, an event is treated as a match if
# EITHER signal is present (OR, not AND). If Cinema City changes one of
# these in the future, the other still catches it.
AUDITORIUM_PATTERN = "imax"       # case-insensitive substring of auditorium name; "" disables this check
ATTRIBUTE_PATTERNS = ["70-mm"]    # case-insensitive attributeIds to treat as a match; [] disables this check

FILM_PATTERN = ""  # case-insensitive substring of film title; "" matches all films

API_BASE = "https://www.cinemacity.cz/cz/data-api-service/v1/quickbook/10101"
STATE_FILE = Path("state/seen.json")

NOTIFY_DEVICE_ID = os.environ.get("NOTIFY_DEVICE_ID")
NOTIFY_DEVICE_TOKEN = os.environ.get("NOTIFY_DEVICE_TOKEN")


def get_json(url):
    print(f"GET {url}")

    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 CinemaCityMonitor/1.0"
        }
    )

    with urlopen(request, timeout=30) as response:
        return json.loads(
            response.read().decode("utf-8")
        )


def get_available_dates():
    today = datetime.now().date()
    horizon = today.toordinal() + HORIZON_DAYS
    horizon_date = datetime.fromordinal(horizon)

    # Note: we deliberately do NOT pass attr=70-mm here even though the API
    # supports server-side attribute filtering (and it would cut down the
    # number of requests significantly). Server-side attr filtering only
    # matches on attributeIds, which would silently exclude IMAX screenings
    # that are only identifiable by auditorium name. Fetching everything and
    # filtering locally is slower but guarantees nothing is missed.
    url = (
        f"{API_BASE}/dates/in-cinema/"
        f"{CINEMA_ID}/until/{horizon_date:%Y-%m-%d}"
        f"?attr=&lang=cs_CZ"
    )

    data = get_json(url)

    return extract_dates(data)


def extract_dates(data):
    if isinstance(data, list):
        values = data

    elif isinstance(data, dict):
        body = data.get("body", data)

        if isinstance(body, list):
            values = body

        elif isinstance(body, dict):
            values = []

            for key in (
                "dates",
                "date",
                "items",
                "results"
            ):
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
            for key in (
                "date",
                "eventDate",
                "eventDateTime",
                "dateTime"
            ):
                candidate = value.get(key)

                if candidate:
                    dates.append(str(candidate)[:10])
                    break

    result = sorted(set(dates))

    if not result:
        # If Cinema City ever changes the response shape (or has a hiccup),
        # every parsing branch above falls through to an empty list rather
        # than raising. That would make the script look "successful" while
        # silently doing nothing. Surface it loudly instead.
        print(
            "WARNING: parsed zero available dates from the API response. "
            "This usually means the Cinema City API response shape has "
            "changed and the parser needs updating -- it should NOT "
            "normally be zero."
        )

    return result


def matches_target_screening(auditorium, attrs):
    auditorium_lower = (auditorium or "").lower()
    attrs_lower = {str(a).lower() for a in (attrs or [])}

    auditorium_match = bool(AUDITORIUM_PATTERN) and (
        AUDITORIUM_PATTERN.lower() in auditorium_lower
    )

    attribute_match = bool(ATTRIBUTE_PATTERNS) and any(
        pattern.lower() in attrs_lower
        for pattern in ATTRIBUTE_PATTERNS
    )

    if not AUDITORIUM_PATTERN and not ATTRIBUTE_PATTERNS:
        # Both checks disabled on purpose -- match everything.
        return True

    return auditorium_match or attribute_match


def get_events_for_date(date):
    url = (
        f"{API_BASE}/film-events/in-cinema/"
        f"{CINEMA_ID}/at-date/{date}"
        f"?attr=&lang=cs_CZ"
    )

    data = get_json(url)

    if not isinstance(data, dict):
        raise RuntimeError(
            "Unexpected Cinema City API response."
        )

    body = data.get("body", data)

    if not isinstance(body, dict):
        raise RuntimeError(
            "Cinema City API body is not an object."
        )

    films = body.get("films", [])
    events = body.get("events", [])

    films_by_id = {
        str(f.get("id")): f
        for f in films
        if isinstance(f, dict)
        and f.get("id") is not None
    }

    result = []

    for event in events:
        if not isinstance(event, dict):
            continue

        film_id = str(event.get("filmId", ""))

        film = films_by_id.get(
            film_id,
            {}
        )

        film_name = film.get(
            "name",
            film_id
        )

        auditorium = str(
            event.get("auditorium") or ""
        )

        attrs = event.get("attributeIds", []) or []

        if not matches_target_screening(auditorium, attrs):
            continue

        if FILM_PATTERN:
            if (
                FILM_PATTERN.lower()
                not in film_name.lower()
            ):
                continue

        event_id = event.get("id")

        if not event_id:
            continue

        # Prefer the API's own working booking link. The previously used
        # tickets.cinemacity.cz/order/... URL scheme is obsolete and returns
        # HTTP 404 -- the current booking flow goes through booking-router.
        booking_link = event.get("bookingRouterLaunchLink") or (
            f"https://www.cinemacity.cz/cz/booking-router/launch/"
            f"{quote(str(event_id))}?lang=cs"
        )

        result.append({
            "id": str(event_id),
            "film": film_name,
            "filmLink": film.get("link"),
            "datetime": event.get("eventDateTime"),
            "auditorium": auditorium,
            "attrs": attrs,
            "bookingLink": booking_link,
            "soldOut": bool(
                event.get("soldOut")
            ),
        })

    return result


def load_state():
    if not STATE_FILE.exists():
        return set()

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as f:
            data = json.load(f)

        if isinstance(data, list):
            return set(
                str(x)
                for x in data
            )

        if isinstance(data, dict):
            return set(
                str(x)
                for x in data.get(
                    "seen",
                    []
                )
            )

    except Exception as exc:
        print(
            f"WARNING: Cannot read state file: {exc}"
        )

    return set()


def save_state(seen):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temp_file = STATE_FILE.with_suffix(
        ".tmp"
    )

    with temp_file.open(
        "w",
        encoding="utf-8"
    ) as f:
        json.dump(
            sorted(seen),
            f,
            ensure_ascii=False,
            indent=2
        )

    temp_file.replace(STATE_FILE)


def send_notification(event):
    if not NOTIFY_DEVICE_ID:
        raise RuntimeError(
            "NOTIFY_DEVICE_ID is missing."
        )

    if not NOTIFY_DEVICE_TOKEN:
        raise RuntimeError(
            "NOTIFY_DEVICE_TOKEN is missing."
        )

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

    extra_text = ""

    if extra:
        extra_text = (
            "\n"
            + " • ".join(extra)
        )

    datetime_text = (
        event.get("datetime")
        or "neznámý čas"
    )

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
        data=json.dumps(
            payload
        ).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "CinemaCityMonitor/1.0",
        },
        method="POST",
    )

    with urlopen(
        request,
        timeout=30
    ) as response:

        response_body = (
            response.read()
            .decode("utf-8")
        )

        print(
            "Notify! response:",
            response.status,
            response_body
        )

        if (
            response.status < 200
            or response.status >= 300
        ):
            raise RuntimeError(
                f"Notify! returned HTTP "
                f"{response.status}"
            )


def main():
    print(
        "========================================"
    )

    print(
        "Cinema City IMAX Monitor"
    )

    print(
        "========================================"
    )

    if (
        not NOTIFY_DEVICE_ID
        or not NOTIFY_DEVICE_TOKEN
    ):
        print(
            "ERROR: Notify! credentials are missing."
        )
        sys.exit(1)

    seen = load_state()

    print(
        f"Previously seen events: {len(seen)}"
    )

    dates = get_available_dates()

    print(
        f"Available dates: {len(dates)}"
    )

    all_events = []

    for date in dates:
        try:
            events = get_events_for_date(
                date
            )

            print(
                f"{date}: "
                f"{len(events)} matching events"
            )

            all_events.extend(events)

        except Exception as exc:
            print(
                f"ERROR while processing "
                f"{date}: {exc}"
            )

    current_ids = set()
    new_events = []

    for event in all_events:
        event_id = event["id"]

        current_ids.add(
            event_id
        )

        if event_id not in seen:
            new_events.append(
                event
            )

    seed_mode = (
        os.environ.get(
            "SEED_MODE"
        ) == "1"
    )

    if seed_mode:
        print(
            "SEED MODE: registering current "
            "events without notifications."
        )

        seen.update(
            current_ids
        )

        save_state(seen)

        print(
            f"Seeded {len(current_ids)} events."
        )

        return

    print(
        f"Current matching events: "
        f"{len(current_ids)}"
    )

    print(
        f"New events: "
        f"{len(new_events)}"
    )

    # Notify per-event, but don't let one failed notification (a brief
    # outage or rate limit on the push service) take down the whole run.
    # If we let an exception propagate here, save_state() below never
    # executes, and any events already notified in this loop would be
    # notified AGAIN next run because they were never marked "seen".
    for event in sorted(
        new_events,
        key=lambda e: (
            e.get("datetime") or "",
            e.get("film") or ""
        )
    ):

        print(
            "NEW:",
            event["film"],
            event.get("datetime"),
            event.get("auditorium")
        )

        try:
            send_notification(
                event
            )

        except Exception as exc:
            print(
                f"ERROR sending notification for "
                f"{event['id']}: {exc}"
            )
            continue

        seen.add(
            event["id"]
        )

    save_state(
        seen
    )

    print(
        "State saved."
    )


if __name__ == "__main__":
    main()
