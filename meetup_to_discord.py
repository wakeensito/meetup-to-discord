#!/usr/bin/env python3
"""Post new Meetup events (and two reminders) to a Discord channel via webhook.

Stdlib only. Reads the group's public iCal feed, keeps state in a JSON file,
and never marks an event as seen until Discord has accepted the post.

    python3 meetup_to_discord.py --seed       # first run: record current events, post nothing
    python3 meetup_to_discord.py              # normal poll
    python3 meetup_to_discord.py --dry-run    # print messages, write nothing
    python3 meetup_to_discord.py --dry-run --now 2026-10-19T10:00
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEFAULT_GROUP = "aws-sbg-at-florida-international-university"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36 meetup-to-discord/1.0"
)
DISCORD_LIMIT = 2000
REMIND_3D = timedelta(days=3)
DAY_OF_HOUR = 8
RECAP_CLOSES_AFTER = timedelta(days=7)
PRUNE_AFTER = timedelta(days=90)
HEARTBEAT_EVERY = timedelta(days=7)
HEARTBEAT_KEY = "_last_checked"

CHECKLIST = """**Marketing & Outreach:**
- ✅ Discord — automated: the bot posts the announcement, 3-day and day-of reminders in #announcements
- [ ] Linktree — add the event link at the top
- [ ] Instagram — feed post + story
- [ ] LinkedIn — post from the group page
- [ ] PantherConnect — create and publish the event

**Tabling & Promotion:**
- [ ] Tabling session — schedule and host 1 on-campus tabling date prior to the workshop
- [ ] Tabling materials — update PowerPoint promo loop & print QR code flyers

**Technical Prep (Workshop Leads):**
- [ ] Workshop slide deck — complete presentation deck
- [ ] Hands-on materials — build & test GitHub lab repo and step-by-step documentation
- [ ] Dry run — complete rehearsal 1 week prior to the workshop

Claim an item by replying with the task + your name!"""


RECAP_CHECKLIST = """**Within 24 hours:**
- ✅ Discord — automated: thank-you + feedback survey post in #announcements
- [ ] Photos — upload to the shared drive and post a few here
- [ ] Slides — post the workshop PowerPoint / PDF in Discord

**Within the week:**
- [ ] Recap — short write-up (what we covered, turnout, what's next) posted in Discord
- [ ] LinkedIn — recap post with photos from the group page
- [ ] Instagram — recap post with photos
- [ ] Lab repo — confirm the GitHub lab repo and docs are public and linked

Claim an item by replying with the task + your name!"""


class PostError(Exception):
    """Discord did not accept the message."""


@dataclass
class Event:
    id: str
    title: str
    start: datetime
    end: datetime | None
    url: str
    description: str
    survey_url: str | None = None


MEMBER_KINDS = {"member_announce", "remind_3d", "remind_day_of", "member_thanks"}
SURVEY_HOSTS = (
    "forms.gle", "docs.google.com/forms", "forms.office.com", "forms.microsoft.com",
    "typeform.com", "surveymonkey.com", "qualtrics.com", "airtable.com", "tally.so", "jotform.com",
)


# ---------------------------------------------------------------- fetching

def fetch(url: str, timeout: int = 20) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def feed_url(group: str) -> str:
    return f"https://www.meetup.com/{group}/events/ical/"


def load_feed(source: str) -> list[Event]:
    """Parse the iCal feed from a URL or a local file (handy for tests and dry runs)."""
    text = fetch(source) if source.startswith(("http://", "https://")) else \
        Path(source).read_text(encoding="utf-8")
    return parse_ical(text)


def event_url(group: str, event_id: str) -> str:
    return f"https://www.meetup.com/{group}/events/{event_id}/"



# ---------------------------------------------------------------- iCal parsing

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        c = value[i]
        if c == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_dt(params: str, value: str) -> datetime:
    tzid = None
    for p in params.split(";"):
        if p.startswith("TZID="):
            tzid = p[5:]
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    elif "T" in value:
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(tzid) if tzid else ET)
    else:  # all-day
        dt = datetime.strptime(value, "%Y%m%d").replace(tzinfo=ET)
    return dt.astimezone(ET)


def parse_ical(text: str) -> list[Event]:
    events: list[Event] = []
    props: dict[str, tuple[str, str]] | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            props = {}
            continue
        if line == "END:VEVENT" and props is not None:
            ev = _event_from_props(props)
            if ev:
                events.append(ev)
            props = None
            continue
        if props is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, _, params = head.partition(";")
        props[name.upper()] = (params, value)
    return events


def _event_from_props(p: dict[str, tuple[str, str]]) -> Event | None:
    if "DTSTART" not in p or "SUMMARY" not in p:
        return None
    url = _unescape(p.get("URL", ("", ""))[1])
    uid = p.get("UID", ("", ""))[1]
    ev_id = _digits(uid) or _url_event_id(url)
    if not ev_id:
        return None
    end = _parse_dt(*p["DTEND"]) if "DTEND" in p else None
    return Event(
        id=ev_id,
        title=_unescape(p["SUMMARY"][1]).strip(),
        start=_parse_dt(*p["DTSTART"]),
        end=end,
        url=url,
        description=_unescape(p.get("DESCRIPTION", ("", ""))[1]),
    )


def _digits(s: str) -> str | None:
    m = re.search(r"\d+", s)
    return m.group(0) if m else None


def _url_event_id(url: str) -> str | None:
    m = re.search(r"/events/(\d+)", url)
    return m.group(1) if m else None


# ---------------------------------------------------------------- location

def extract_room(description: str) -> str | None:
    m = re.search(r"Room:\s*([^\n*]+)", description)
    return m.group(1).strip() if m else None


def parse_venue(html: str) -> str | None:
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or not str(item.get("@type", "")).endswith("Event"):
                continue
            loc = item.get("location")
            for place in loc if isinstance(loc, list) else [loc]:
                if isinstance(place, dict) and place.get("@type") == "Place" and place.get("name"):
                    return str(place["name"]).strip()
    return None


def fetch_venue(event_url: str) -> str | None:
    return parse_venue(fetch(event_url))


def format_location(venue: str | None, room: str | None) -> str | None:
    parts = [venue] if venue else []
    if room:
        parts.append(f"Room: {room}")
    return " · ".join(parts) if parts else None


def resolve_location(ev: Event, venue_lookup: Callable[[str], str | None]) -> str | None:
    try:
        venue = venue_lookup(ev.url)
    except Exception as exc:  # best-effort enrichment only
        print(f"warn: venue lookup failed for {ev.url}: {exc}", file=sys.stderr)
        venue = None
    return format_location(venue, extract_room(ev.description))


def blurb(description: str, limit: int = 300) -> str | None:
    """First real paragraph of the Meetup description, bold stripped, capped at a word."""
    for para in re.split(r"\n\s*\n", description or ""):
        lines = [ln.strip() for ln in para.splitlines() if ln.strip()]
        if not lines:
            continue
        # Meetup prefixes the description with the group name on a line of its own.
        if len(lines) > 1 and len(lines[0]) <= 80 and not re.search(r"[.!?:]$", lines[0]):
            lines = lines[1:]
        text = " ".join(lines).replace("**", "").strip()
        if not text or re.match(r"Room:", text):
            continue
        if len(text) > limit:
            text = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
        return text
    return None


def find_survey_url(description: str) -> str | None:
    for m in re.finditer(r"https?://[^\s<>()\[\]]+", description or ""):
        url = m.group(0).rstrip(".,;:!?")
        if any(host in url for host in SURVEY_HOSTS):
            return url
    return None


# ---------------------------------------------------------------- formatting

def _clock(dt: datetime) -> str:
    return dt.astimezone(ET).strftime("%I:%M %p").lstrip("0")


def _date_words(dt: datetime) -> str:
    dt = dt.astimezone(ET)
    return f"{dt.strftime('%A')}, {dt.strftime('%B')} {dt.day}"


def role_mention(role_id: str | None) -> str | None:
    role_id = (role_id or "").strip()
    return f"<@&{role_id}>" if role_id else None


def _trail(line: str, mention: str | None) -> str:
    return f"{line} {mention}" if mention else line


def _cap(text: str) -> str:
    return text if len(text) <= DISCORD_LIMIT else text[: DISCORD_LIMIT - 10].rstrip() + "…"


def format_announcement(ev: Event, location: str | None, mention: str | None = None) -> str:
    when = f"📅 {_date_words(ev.start)} · {_clock(ev.start)}"
    if ev.end:
        when += f"–{_clock(ev.end)}"
    when += " ET"
    lines = ["📣 **New event is up on Meetup!**", "", f"**{ev.title}**", when]
    if location:
        lines.append(f"📍 {location}")
    lines += [f"🔗 {ev.url}", "", _trail(CHECKLIST, mention)]
    return _cap("\n".join(lines))


def format_reminder(ev: Event, kind: str, location: str | None, mention: str | None = None) -> str:
    if kind == "remind_day_of":
        first = f"📣 **Today!** {ev.title} starts at {_clock(ev.start)} ET"
    else:
        first = f"⏰ **Reminder:** {ev.title} is {_date_words(ev.start)} at {_clock(ev.start)} ET"
    lines = [first]
    if location:
        lines.append(f"📍 {location}")
    lines.append(f"🔗 {ev.url}")
    if mention:
        lines.append(mention)
    return _cap("\n".join(lines))


def _when_line(ev: Event) -> str:
    when = f"🕒 {_clock(ev.start)}"
    if ev.end:
        when += f"–{_clock(ev.end)}"
    return when + " ET"


def _blocks(*blocks: str | None) -> str:
    return _cap("\n\n".join(b for b in blocks if b))


def format_member_announcement(ev: Event, location: str | None, blurb_text: str | None,
                               mention: str | None = None) -> str:
    details = [f"📅 {_date_words(ev.start)}", _when_line(ev)]
    if location:
        details.append(f"📍 {location}")
    return _blocks(f"📣 **New event: {ev.title}**", "\n".join(details), blurb_text,
                   f"🔗 RSVP on Meetup: {ev.url}", mention)


def format_member_reminder(ev: Event, kind: str, location: str | None,
                           mention: str | None = None) -> str:
    if kind == "remind_day_of":
        head = f"🚀 **Today: {ev.title}**"
        details = [_when_line(ev)]
        tail = f"See you there! 🔗 {ev.url}"
    else:
        head = f"⏰ **Coming up {ev.start.astimezone(ET).strftime('%A')}: {ev.title}**"
        details = [f"📅 {_date_words(ev.start)}", _when_line(ev)]
        tail = f"Haven't RSVP'd yet? 🔗 {ev.url}"
    if location:
        details.append(f"📍 {location}")
    return _blocks(head, "\n".join(details), tail, mention)


def format_member_thanks(ev: Event, survey_url: str | None, mention: str | None = None) -> str:
    ask = "We'd love your feedback. It takes a minute and shapes the next one."
    if survey_url:
        how, link = f"📝 Feedback survey: {survey_url}", f"🔗 Event page: {ev.url}"
    else:
        # Meetup's /feedback/ page is organizer-only; members use the Feedback button on the event page.
        how, link = "📝 Open the event page and tap **Feedback** to leave your review:", f"🔗 {ev.url}"
    return _blocks(f"🙌 **Thank you for coming to {ev.title}!**", ask, how, link, mention)


def format_recap(ev: Event, mention: str | None = None) -> str:
    lines = [f"✅ **{ev.title} is a wrap! Post-event checklist**", f"🔗 {ev.url}", "",
             _trail(RECAP_CHECKLIST, mention)]
    return _cap("\n".join(lines))


# ---------------------------------------------------------------- Discord

def post_discord(webhook_url: str, content: str, retries: int = 2,
                 role_id: str | None = None) -> None:
    sep = "&" if "?" in webhook_url else "?"
    url = f"{webhook_url}{sep}wait=true"
    allowed: dict = {"parse": []}  # never @everyone/@here or users from event text
    if role_id:
        allowed["roles"] = [role_id]
    body = json.dumps({"content": content, "allowed_mentions": allowed}).encode()
    req = Request(url, data=body, method="POST",
                  headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urlopen(req, timeout=20) as resp:
                if 200 <= resp.status < 300:
                    return
                raise PostError(f"Discord returned HTTP {resp.status}")
        except HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code == 429 and attempt < retries:
                try:
                    wait = float(json.loads(detail).get("retry_after", 1))
                except (ValueError, AttributeError):
                    wait = 1.0
                time.sleep(wait)
                continue
            raise PostError(f"Discord returned HTTP {e.code}: {detail[:200]}") from e
        except URLError as e:
            raise PostError(f"Discord unreachable: {e}") from e
    raise PostError("Discord rate limit retries exhausted")


# ---------------------------------------------------------------- state

def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8") or "{}")


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _day_of_open(start: datetime) -> datetime:
    """8:00 AM ET on the event day, or midnight if the event starts before 8."""
    start = start.astimezone(ET)
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    eight = midnight.replace(hour=DAY_OF_HOUR)
    return eight if start > eight else midnight


def _morning_of(day: datetime) -> datetime:
    return day.astimezone(ET).replace(hour=DAY_OF_HOUR, minute=0, second=0, microsecond=0)


def _3d_open(start: datetime) -> datetime:
    """8:00 AM ET three days before the event day."""
    return _morning_of(start - REMIND_3D)


def _in_3d_window(start: datetime, now: datetime) -> bool:
    return _3d_open(start) <= now < start


def _in_day_of_window(start: datetime, now: datetime) -> bool:
    return _day_of_open(start) <= now < start


def _recap_open(start: datetime) -> datetime:
    """8:00 AM ET the morning after the event day."""
    return _morning_of(start + timedelta(days=1))


def _in_recap_window(start: datetime, now: datetime) -> bool:
    return _recap_open(start) <= now < start + RECAP_CLOSES_AFTER


def _event_from_state(event_id: str, entry: dict, group: str) -> Event | None:
    try:
        start = datetime.fromisoformat(entry["start"]).astimezone(ET)
    except (KeyError, TypeError, ValueError):
        return None
    return Event(id=event_id, title=str(entry.get("title") or "Event"), start=start, end=None,
                 url=entry.get("url") or event_url(group, event_id), description="",
                 survey_url=entry.get("survey_url"))


def decide_actions(events: Iterable[Event], state: dict, now: datetime,
                   group: str = DEFAULT_GROUP) -> list[tuple[Event, str]]:
    actions: list[tuple[Event, str]] = []
    for ev in events:
        entry = state.get(ev.id) or {}
        if not entry.get("announced"):
            actions.append((ev, "announce"))            # E-board task post
        if not entry.get("member_announced"):
            actions.append((ev, "member_announce"))     # #announcements
        if entry.get("announced") or entry.get("member_announced"):
            if not entry.get("reminded_day_of") and _in_day_of_window(ev.start, now):
                actions.append((ev, "remind_day_of"))
            elif not entry.get("reminded_3d") and _in_3d_window(ev.start, now):
                actions.append((ev, "remind_3d"))
    # Past events fall out of the feed, so the morning-after posts come from saved state.
    for event_id, entry in state.items():
        if event_id.startswith("_") or not isinstance(entry, dict):
            continue
        ev = _event_from_state(event_id, entry, group)
        if not ev or not _in_recap_window(ev.start, now):
            continue
        if entry.get("announced") and not entry.get("posted_recap"):
            actions.append((ev, "post_event"))
        if (entry.get("announced") or entry.get("member_announced")) \
                and not entry.get("member_thanked"):
            actions.append((ev, "member_thanks"))
    return actions


def apply_action(state: dict, ev: Event, kind: str, now: datetime) -> None:
    entry = state.setdefault(ev.id, {})
    entry["title"] = ev.title
    entry["start"] = ev.start.isoformat()
    if ev.url:
        entry["url"] = ev.url
    for flag in ("posted_recap", "member_announced", "member_thanked"):
        entry.setdefault(flag, False)
    survey = find_survey_url(ev.description)
    if survey:
        entry["survey_url"] = survey
    if kind in ("announce", "member_announce"):
        entry["announced" if kind == "announce" else "member_announced"] = True
        # Announcing inside a reminder window supersedes that reminder.
        entry["reminded_3d"] = entry.get("reminded_3d", False) or now >= _3d_open(ev.start)
        entry["reminded_day_of"] = entry.get("reminded_day_of", False) or now >= _day_of_open(ev.start)
    elif kind == "member_thanks":
        entry["member_thanked"] = True
    elif kind == "remind_day_of":
        entry["reminded_day_of"] = True
        entry["reminded_3d"] = True
    elif kind == "remind_3d":
        entry["reminded_3d"] = True
    elif kind == "post_event":
        entry["posted_recap"] = True
    else:
        raise ValueError(kind)


def prune_state(state: dict, feed_ids: set[str], now: datetime) -> None:
    for key in list(state):
        if key.startswith("_") or key in feed_ids:
            continue
        try:
            start = datetime.fromisoformat(state[key]["start"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < now - PRUNE_AFTER:
            del state[key]


def touch_heartbeat(state: dict, now: datetime) -> bool:
    last = state.get(HEARTBEAT_KEY)
    try:
        if last and now - datetime.fromisoformat(last) < HEARTBEAT_EVERY:
            return False
    except ValueError:
        pass
    state[HEARTBEAT_KEY] = now.replace(microsecond=0).isoformat()
    return True


# ---------------------------------------------------------------- orchestration

def run(events: list[Event], state_path: Path, now: datetime, team_poster: Callable[[str], None] | None,
        *, seed: bool = False, dry_run: bool = False,
        venue_lookup: Callable[[str], str | None] | None = None,
        mention_role_id: str | None = None, group: str = DEFAULT_GROUP,
        member_poster: Callable[[str], None] | None = None,
        announce_role_id: str | None = None) -> int:
    venue_lookup = venue_lookup or fetch_venue
    state = load_state(state_path)
    team_mention = role_mention(mention_role_id)
    member_mention = role_mention(announce_role_id)
    feed_ids = {ev.id for ev in events}

    # Backfill fields for entries written by older versions. Member-facing flags default to
    # "already done" so members are not retro-announced or thanked for old events.
    for event_id, entry in state.items():
        if not event_id.startswith("_") and isinstance(entry, dict):
            entry.setdefault("posted_recap", False)
            entry.setdefault("member_announced", True)
            entry.setdefault("member_thanked", True)
    for ev in events:
        entry = state.get(ev.id)
        if isinstance(entry, dict):
            entry.setdefault("url", ev.url)
            survey = find_survey_url(ev.description)   # organisers may add it after publishing
            if survey:
                entry["survey_url"] = survey

    if seed:
        for ev in events:
            if ev.id not in state:
                state[ev.id] = {"announced": True, "member_announced": True,
                                "reminded_3d": False, "reminded_day_of": False,
                                "posted_recap": False, "member_thanked": False,
                                "title": ev.title, "start": ev.start.isoformat(), "url": ev.url,
                                **({"survey_url": s} if (s := find_survey_url(ev.description)) else {})}
        touch_heartbeat(state, now)
        if not dry_run:
            save_state(state_path, state)
        print(f"seeded {len(events)} event(s); nothing posted")
        return 0

    actions = decide_actions(events, state, now, group)
    failed = 0
    locations: dict[str, str | None] = {}
    for ev, kind in actions:
        to_members = kind in MEMBER_KINDS
        poster = member_poster if to_members else team_poster
        channel = "#announcements" if to_members else "E-board"
        if poster is None and not dry_run:
            var = "DISCORD_ANNOUNCE_WEBHOOK_URL" if to_members else "DISCORD_WEBHOOK_URL"
            print(f"warn: skipping {kind} for {ev.id}: {var} is not set", file=sys.stderr)
            continue
        if kind in ("post_event", "member_thanks"):
            location = None
        elif ev.id in locations:
            location = locations[ev.id]
        else:
            location = locations[ev.id] = resolve_location(ev, venue_lookup)
        if kind == "announce":
            content = format_announcement(ev, location, team_mention)
        elif kind == "member_announce":
            content = format_member_announcement(ev, location, blurb(ev.description), member_mention)
        elif kind in ("remind_3d", "remind_day_of"):
            content = format_member_reminder(ev, kind, location, member_mention)
        elif kind == "post_event":
            content = format_recap(ev, team_mention)
        else:
            content = format_member_thanks(ev, ev.survey_url, member_mention)
        if dry_run:
            print(f"--- would post [{kind} → {channel}] for {ev.id} ({len(content)} chars) ---\n{content}\n")
            continue
        try:
            poster(content)
        except PostError as exc:
            failed += 1
            print(f"error: {kind} for {ev.id} failed: {exc}", file=sys.stderr)
            continue
        apply_action(state, ev, kind, now)
        save_state(state_path, state)
        print(f"posted {kind} → {channel} for {ev.id}: {ev.title}")

    if dry_run:
        if not actions:
            print("nothing to post")
        return 0

    changed = False
    before = json.dumps(state, sort_keys=True)
    prune_state(state, feed_ids, now)
    if not actions:
        touch_heartbeat(state, now)
    changed = json.dumps(state, sort_keys=True) != before
    if changed or not state_path.exists():
        save_state(state_path, state)
    if not actions:
        print("nothing to post")
    return 1 if failed else 0


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=ET)
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=ET) if dt.tzinfo is None else dt.astimezone(ET)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", action="store_true", help="record current events without posting")
    ap.add_argument("--dry-run", action="store_true", help="print messages; post and write nothing")
    ap.add_argument("--state", default="seen_events.json", help="path to the state file")
    ap.add_argument("--now", help="override the clock (ISO 8601, naive = America/New_York)")
    ap.add_argument("--group", default=os.environ.get("MEETUP_GROUP", DEFAULT_GROUP))
    ap.add_argument("--mention-role", default=os.environ.get("DISCORD_MENTION_ROLE_ID", ""),
                    help="role ID to @mention at the end of E-board posts")
    ap.add_argument("--announce-role", default=os.environ.get("DISCORD_ANNOUNCE_ROLE_ID", ""),
                    help="role ID to @mention at the end of #announcements posts")
    ap.add_argument("--feed", help="iCal URL or local .ics file (default: the group's feed)")
    args = ap.parse_args(argv)

    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    announce_hook = os.environ.get("DISCORD_ANNOUNCE_WEBHOOK_URL", "").strip()
    if not (webhook or announce_hook) and not (args.dry_run or args.seed):
        print("error: neither DISCORD_WEBHOOK_URL nor DISCORD_ANNOUNCE_WEBHOOK_URL is set "
              "(use --dry-run to test without them)", file=sys.stderr)
        return 2

    try:
        events = load_feed(args.feed or feed_url(args.group))
    except (URLError, HTTPError, OSError, ValueError) as exc:
        print(f"error: could not fetch/parse the Meetup iCal feed: {exc}", file=sys.stderr)
        return 1
    print(f"feed: {len(events)} event(s) for {args.group}")

    role_id = args.mention_role.strip() or None
    announce_role = args.announce_role.strip() or None
    team_poster = (lambda content: post_discord(webhook, content, role_id=role_id)) if webhook \
        else ((lambda c: None) if (args.dry_run or args.seed) else None)
    member_poster = (lambda content: post_discord(announce_hook, content, role_id=announce_role)) \
        if announce_hook else None
    return run(events, Path(args.state), parse_now(args.now), team_poster,
               seed=args.seed, dry_run=args.dry_run, mention_role_id=role_id, group=args.group,
               member_poster=member_poster, announce_role_id=announce_role)


if __name__ == "__main__":
    sys.exit(main())
