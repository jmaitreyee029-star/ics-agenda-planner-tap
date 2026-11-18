#!/usr/bin/env python3
"""ics-agenda-planner: agenda, conflicts and free slots from an iCalendar (.ics) file or URL.

Standard library only. Handles folded lines, TZID / UTC / floating times, all-day events,
and simple weekly/daily RRULEs (COUNT/UNTIL/BYDAY). Output is JSON (default) or Markdown.

  python3 ics_agenda.py --ics calendar.ics --date 2026-11-20 --tz America/Los_Angeles --format md
  python3 ics_agenda.py --ics "$CALENDAR_ICS_URL" --days 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def read_ics(src: str) -> str:
    if re.match(r"^(https?|webcal)://", src):
        url = src.replace("webcal://", "https://", 1)
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    with open(src, encoding="utf-8") as f:
        return f.read()


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def parse_dt(value: str, params: dict, default_tz: ZoneInfo) -> tuple[datetime, bool]:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=default_tz), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC")), False
    tz = ZoneInfo(params["TZID"]) if "TZID" in params else default_tz
    return datetime.strptime(value[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz), False


def parse_events(text: str, tz: ZoneInfo) -> list[dict]:
    events, cur = [], None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            events.append(cur)
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, *ps = head.split(";")
            params = dict(p.split("=", 1) for p in ps if "=" in p)
            if name in ("DTSTART", "DTEND"):
                cur[name], cur[name + "_allday"] = parse_dt(value, params, tz)
            elif name in ("SUMMARY", "LOCATION", "RRULE", "STATUS", "UID"):
                cur[name] = value.replace("\\,", ",").replace("\\n", " ")
    out = []
    for e in events:
        if "DTSTART" not in e or e.get("STATUS") == "CANCELLED":
            continue
        start = e["DTSTART"]
        end = e.get("DTEND") or (start + (timedelta(days=1) if e["DTSTART_allday"] else timedelta(hours=1)))
        out.append({"title": e.get("SUMMARY", "(no title)"), "location": e.get("LOCATION", ""),
                    "start": start, "end": end, "all_day": e["DTSTART_allday"], "rrule": e.get("RRULE", "")})
    return out


def expand(events: list[dict], lo: datetime, hi: datetime) -> list[dict]:
    result = []
    for e in events:
        occ = [(e["start"], e["end"])]
        if e["rrule"]:
            rule = dict(p.split("=", 1) for p in e["rrule"].split(";") if "=" in p)
            step = {"DAILY": timedelta(days=1), "WEEKLY": timedelta(days=1)}.get(rule.get("FREQ"))
            if step:
                count = int(rule.get("COUNT", "500"))
                until = parse_dt(rule["UNTIL"], {}, e["start"].tzinfo)[0] if "UNTIL" in rule else hi
                byday = {DAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in DAYS}
                if rule["FREQ"] == "WEEKLY" and not byday:
                    byday = {e["start"].weekday()}
                dur, occ, s, n = e["end"] - e["start"], [], e["start"], 0
                while s <= min(until, hi) and n < count:
                    if rule["FREQ"] == "DAILY" or s.weekday() in byday:
                        occ.append((s, s + dur))
                        n += 1
                    s += step
        for s, en in occ:
            if en > lo and s < hi:
                result.append({**e, "start": s, "end": en})
    return sorted(result, key=lambda x: x["start"])


def free_slots(evts, day_lo, work_start, work_end, min_minutes):
    cursor, stop = day_lo.replace(hour=work_start), day_lo.replace(hour=work_end)
    slots = []
    for e in (x for x in evts if not x["all_day"]):
        if e["end"] <= cursor or e["start"] >= stop:
            continue
        if (e["start"] - cursor) >= timedelta(minutes=min_minutes):
            slots.append((cursor, e["start"]))
        cursor = max(cursor, e["end"])
    if (stop - cursor) >= timedelta(minutes=min_minutes):
        slots.append((cursor, stop))
    return slots


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ics", required=True, help="path or https/webcal URL of an .ics feed")
    ap.add_argument("--date", default=None, help="first day YYYY-MM-DD (default today)")
    ap.add_argument("--days", type=int, default=1)
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--work-start", type=int, default=9)
    ap.add_argument("--work-end", type=int, default=18)
    ap.add_argument("--min-minutes", type=int, default=30)
    ap.add_argument("--format", choices=("json", "md"), default="json")
    a = ap.parse_args()
    tz = ZoneInfo(a.tz)
    first = date.fromisoformat(a.date) if a.date else datetime.now(tz).date()
    events = parse_events(read_ics(a.ics), tz)
    report = []
    for i in range(a.days):
        lo = datetime.combine(first + timedelta(days=i), time(0), tz)
        hi = lo + timedelta(days=1)
        day = [{**e, "start": e["start"].astimezone(tz), "end": e["end"].astimezone(tz)} for e in expand(events, lo, hi)]
        timed = [e for e in day if not e["all_day"]]
        conflicts = [(x["title"], y["title"]) for j, x in enumerate(timed) for y in timed[j + 1:] if y["start"] < x["end"]]
        slots = free_slots(timed, lo, a.work_start, a.work_end, a.min_minutes)
        report.append({"date": lo.date().isoformat(),
                       "events": [{"title": e["title"], "start": e["start"].isoformat(), "end": e["end"].isoformat(),
                                   "all_day": e["all_day"], "location": e["location"]} for e in day],
                       "conflicts": [{"a": x, "b": y} for x, y in conflicts],
                       "free_slots": [{"start": s.isoformat(), "end": e.isoformat(),
                                       "minutes": int((e - s).total_seconds() // 60)} for s, e in slots]})
    if a.format == "json":
        json.dump({"timezone": a.tz, "days": report}, sys.stdout, indent=1)
        print()
        return
    for d in report:
        print(f"## {d['date']}")
        for e in d["events"]:
            t = "all day" if e["all_day"] else f"{e['start'][11:16]}–{e['end'][11:16]}"
            print(f"- {t} {e['title']}" + (f" ({e['location']})" if e["location"] else ""))
        for c in d["conflicts"]:
            print(f"- ⚠️ conflict: {c['a']} overlaps {c['b']}")
        print("- free: " + (", ".join(f"{s['start'][11:16]}–{s['end'][11:16]} ({s['minutes']}m)"
                                       for s in d["free_slots"]) or "none"))


if __name__ == "__main__":
    main()
