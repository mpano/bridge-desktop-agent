"""Plain-text views of connected-service results, shown only to the local user."""

from datetime import datetime


def _clip(text, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _when(value: dict | None) -> str:
    value = value or {}
    if "dateTime" in value:
        moment = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
        return moment.strftime("%a %d %b %H:%M")
    if "date" in value:
        return datetime.fromisoformat(value["date"]).strftime("%a %d %b") + " (all day)"
    return ""


def _time(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%a %d %b %H:%M")


def _more(data: dict) -> str:
    return "\n…more results exist; narrow the search to see them." if data.get("has_more") else ""


def accounts(data: dict) -> str:
    rows = [
        f"• {item['provider'].replace('_', ' ')}: {item['identity']}" for item in data["accounts"]
    ]
    return "Connected accounts:\n" + "\n".join(rows) if rows else "No accounts are connected."


def email_search(data: dict) -> str:
    if not data["messages"]:
        return "No matching emails."
    rows = [
        f"• {_clip(item['subject'] or '(no subject)', 90)}\n"
        f"  {_clip(item['from'], 60)} · {_clip(item['date'], 32)}\n"
        f"  {_clip(item['snippet'], 160)}"
        for item in data["messages"]
    ]
    return "Emails:\n" + "\n".join(rows) + _more(data)


def email_thread(data: dict) -> str:
    rows = [
        f"From {_clip(item['from'], 60)} · {_clip(item['date'], 32)}\n"
        f"Subject: {_clip(item['subject'], 120)}\n{str(item['body']).strip()[:1500]}"
        for item in data["messages"]
    ]
    return "\n\n———\n\n".join(rows) or "The thread is empty."


def email_written(data: dict) -> str:
    return f"✓ {data['message']} To: {', '.join(data.get('to', []))}"


def calendars(data: dict) -> str:
    rows = [
        f"• {item['summary']} ({item.get('timeZone') or 'no timezone'})"
        for item in data["calendars"]
    ]
    return "Calendars:\n" + "\n".join(rows)


def events(data: dict) -> str:
    if not data["events"]:
        return "No events in that time."
    rows = []
    for item in data["events"]:
        line = f"• {_when(item.get('start'))} — {_clip(item.get('summary') or '(no title)', 80)}"
        if item.get("location"):
            line += f" @ {_clip(item['location'], 50)}"
        rows.append(line)
    return "Events:\n" + "\n".join(rows) + _more(data)


def free_slots(data: dict) -> str:
    if not data["free"]:
        return "No free time found in that window."
    rows = [f"• {_time(item['start'])} → {_time(item['end'])[-5:]}" for item in data["free"]]
    note = "" if data.get("complete") else "\n(Calendar had more events than fit; double-check.)"
    return "Free time:\n" + "\n".join(rows) + note


def event_written(data: dict) -> str:
    title = data.get("title") or "event"
    start = data.get("start")
    when = _when(start) if isinstance(start, dict) else _time(start) if start else ""
    return f"✓ {data['message']} {title} {when}".strip()


def slack_messages(data: dict) -> str:
    if not data["messages"]:
        return "No Slack messages found."
    header = f"Slack {data['channel']}:" if data.get("channel") else "Slack messages:"
    rows = []
    for item in data["messages"]:
        where = f" in #{item['channel']}" if item.get("channel") else ""
        rows.append(f"• {item.get('user') or 'someone'}{where}: {_clip(item.get('text'), 240)}")
    return header + "\n" + "\n".join(rows) + _more(data)


def slack_channels(data: dict) -> str:
    names = sorted("#" + item["name"] for item in data["channels"] if item.get("name"))
    return "Slack channels: " + ", ".join(names) if names else "No Slack channels found."


def slack_sent(data: dict) -> str:
    return f"✓ {data['message']}"


def spotify_items(data: dict) -> str:
    rows = []
    for item in data.get("items") or data.get("playlists") or []:
        artists = ", ".join(item.get("artists") or [])
        rows.append(f"• {item['name']}" + (f" — {artists}" if artists else ""))
    return "\n".join(rows) or "Nothing found on Spotify."


def spotify_devices(data: dict) -> str:
    rows = [
        f"• {item.get('name')} ({item.get('type')}){' — active' if item.get('is_active') else ''}"
        for item in data["devices"]
    ]
    return "Spotify devices:\n" + "\n".join(rows) if rows else "No Spotify devices are available."


def spotify_playing(data: dict) -> str:
    return f"♫ Playing {data['playing']} — {', '.join(data['artists'])}"
