"""
outlook_calendar.py — James's Outlook calendar through Microsoft Graph.

Read: list events, find open time.
Write: create / move / cancel events. Writes are only ever called by the
agent after James confirms (rowan_agent enforces this), because an event
with attendees sends real invitations.

All times are Eastern. Needs the Calendars.ReadWrite delegated permission
(re-run outlook_auth.py once after adding it in Azure).
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests

from outlook_mail import GRAPH, get_access_token

CAL_SCOPES = ["Calendars.ReadWrite"]
TZ = ZoneInfo("America/New_York")
GRAPH_TZ = "Eastern Standard Time"          # Windows zone name; covers DST
WORKDAY_START = time(8, 0)
WORKDAY_END = time(18, 0)


def _headers() -> dict:
    token = get_access_token(scopes=CAL_SCOPES)
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Prefer": f'outlook.timezone="{GRAPH_TZ}"',
    }


def _check(resp, what):
    if resp.status_code >= 400:
        raise RuntimeError(f"Calendar {what} failed [{resp.status_code}]: {resp.text[:300]}")
    return resp


def _parse_local(value: str) -> datetime:
    """'2026-09-24T14:00' / '2026-09-24 14:00' / '2026-09-24' -> aware Eastern datetime."""
    value = (value or "").strip().replace(" ", "T")
    if not value:
        raise ValueError("A date/time is required.")
    dt = datetime.fromisoformat(value[:19])
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def _graph_dt(dt: datetime) -> dict:
    return {"dateTime": dt.astimezone(TZ).strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": GRAPH_TZ}


def _from_graph(obj: dict) -> datetime:
    # With the Prefer header, Graph returns Eastern wall-clock time.
    raw = (obj or {}).get("dateTime", "")[:19]
    return datetime.fromisoformat(raw).replace(tzinfo=TZ)


def _simplify(ev: dict) -> dict:
    start, end = _from_graph(ev.get("start")), _from_graph(ev.get("end"))
    return {
        "event_id": ev.get("id"),
        "subject": ev.get("subject") or "(no subject)",
        "start": start.strftime("%Y-%m-%d %H:%M"),
        "end": end.strftime("%Y-%m-%d %H:%M"),
        "all_day": bool(ev.get("isAllDay")),
        "location": ((ev.get("location") or {}).get("displayName") or ""),
        "show_as": ev.get("showAs"),
        "organizer": (((ev.get("organizer") or {}).get("emailAddress") or {}).get("name") or ""),
        "attendees": [
            ((a.get("emailAddress") or {}).get("name") or (a.get("emailAddress") or {}).get("address"))
            for a in (ev.get("attendees") or [])
        ][:10],
        "is_cancelled": bool(ev.get("isCancelled")),
        "online_meeting": bool(ev.get("isOnlineMeeting")),
    }


def list_events(start_date: str, end_date: str = None) -> list:
    """Events from the start of start_date through the end of end_date (inclusive)."""
    start = _parse_local(start_date).replace(hour=0, minute=0, second=0)
    end_day = _parse_local(end_date or start_date)
    end = end_day.replace(hour=0, minute=0, second=0) + timedelta(days=1)
    events, url = [], f"{GRAPH}/me/calendarView"
    params = {
        "startDateTime": start.isoformat(),
        "endDateTime": end.isoformat(),
        "$orderby": "start/dateTime",
        "$top": 100,
        "$select": "id,subject,start,end,isAllDay,location,showAs,organizer,attendees,isCancelled,isOnlineMeeting",
    }
    headers = _headers()
    while url and len(events) < 300:
        resp = _check(requests.get(url, headers=headers, params=params, timeout=30), "read")
        data = resp.json()
        events.extend(data.get("value", []))
        url, params = data.get("@odata.nextLink"), None
    return [_simplify(e) for e in events if not e.get("isCancelled")]


def find_free_time(start_date: str, end_date: str = None, duration_minutes: int = 60,
                   day_start: str = None, day_end: str = None) -> list:
    """Open blocks of at least duration_minutes on weekdays inside working hours."""
    ws = time.fromisoformat(day_start) if day_start else WORKDAY_START
    we = time.fromisoformat(day_end) if day_end else WORKDAY_END
    first = _parse_local(start_date).date()
    last = _parse_local(end_date or start_date).date()
    busy = [
        (_parse_local(e["start"]), _parse_local(e["end"]))
        for e in list_events(first.isoformat(), last.isoformat())
        if e["show_as"] not in ("free", "workingElsewhere")
    ]
    now = datetime.now(TZ)
    need = timedelta(minutes=int(duration_minutes or 60))
    slots = []
    day = first
    while day <= last:
        if day.weekday() < 5:
            cursor = datetime.combine(day, ws, TZ)
            end_of_day = datetime.combine(day, we, TZ)
            if cursor < now:
                # Round up to the next half hour.
                cursor = now + timedelta(minutes=(30 - now.minute % 30) % 30)
                cursor = cursor.replace(second=0, microsecond=0)
            for b_start, b_end in sorted(busy):
                if b_end <= cursor or b_start >= end_of_day:
                    continue
                if b_start - cursor >= need:
                    slots.append((cursor, b_start))
                cursor = max(cursor, b_end)
            if end_of_day - cursor >= need:
                slots.append((cursor, end_of_day))
        day += timedelta(days=1)
    return [
        {"start": s.strftime("%Y-%m-%d %H:%M"), "end": e.strftime("%Y-%m-%d %H:%M"),
         "minutes": int((e - s).total_seconds() // 60)}
        for s, e in slots
    ][:40]


def create_event(subject: str, start: str, end: str = None, duration_minutes: int = None,
                 location: str = None, attendees: list = None, body: str = None,
                 online_meeting: bool = False, show_as: str = None) -> dict:
    s = _parse_local(start)
    e = _parse_local(end) if end else s + timedelta(minutes=int(duration_minutes or 60))
    payload = {
        "subject": subject,
        "start": _graph_dt(s),
        "end": _graph_dt(e),
    }
    if location:
        payload["location"] = {"displayName": location}
    if body:
        payload["body"] = {"contentType": "Text", "content": body}
    if attendees:
        payload["attendees"] = [
            {"emailAddress": {"address": a}, "type": "required"} for a in attendees if a
        ]
    if online_meeting:
        payload["isOnlineMeeting"] = True
        payload["onlineMeetingProvider"] = "teamsForBusiness"
    if show_as:
        payload["showAs"] = show_as
    resp = _check(requests.post(f"{GRAPH}/me/events", headers=_headers(), json=payload, timeout=30), "create")
    return _simplify(resp.json())


def update_event(event_id: str, subject: str = None, start: str = None, end: str = None,
                 location: str = None) -> dict:
    headers = _headers()
    patch = {}
    if subject:
        patch["subject"] = subject
    if location is not None:
        patch["location"] = {"displayName": location}
    if start:
        s = _parse_local(start)
        if end:
            e = _parse_local(end)
        else:
            # Keep the original length when only the start moves.
            cur = _check(requests.get(f"{GRAPH}/me/events/{event_id}", headers=headers,
                                      params={"$select": "start,end"}, timeout=30), "read").json()
            e = s + (_from_graph(cur["end"]) - _from_graph(cur["start"]))
        patch["start"], patch["end"] = _graph_dt(s), _graph_dt(e)
    elif end:
        patch["end"] = _graph_dt(_parse_local(end))
    if not patch:
        raise ValueError("Nothing to change.")
    resp = _check(requests.patch(f"{GRAPH}/me/events/{event_id}", headers=headers, json=patch, timeout=30), "update")
    return _simplify(resp.json())


def delete_event(event_id: str) -> None:
    """Deletes the event. If James organized it with attendees, Graph sends cancellations."""
    _check(requests.delete(f"{GRAPH}/me/events/{event_id}", headers=_headers(), timeout=30), "delete")
